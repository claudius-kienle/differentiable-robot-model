# Copyright (c) Facebook, Inc. and its affiliates.
"""
Differentiable robot model class
====================================
"""

import logging
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
import os

import torch

from .rigid_body import (
    DifferentiableRigidBody,
)
from .spatial_vector_algebra import SpatialMotionVec, SpatialForceVec
from .urdf_utils import URDFRobotModel

logger = logging.getLogger(__name__)

def tensor_check(function):
    """
    A decorator for checking the device of input tensors
    """

    @dataclass
    class BatchInfo:
        shape: torch.Size = torch.Size([])
        init: bool = False

    def preprocess(arg, obj, batch_info):
        if type(arg) is torch.Tensor:
            # Check device
            assert (
                arg.device.type == obj._device.type
            ), f"Input argument of different device as module: {arg}"

            # Check dimensions & convert to 2-dim tensors
            assert arg.ndim in [1, 2], f"Input tensors must have ndim of 1 or 2."

            if batch_info.init:
                assert (
                    batch_info.shape == arg.shape[:-1]
                ), "Batch size mismatch between input tensors."
            else:
                batch_info.init = True
                batch_info.shape = arg.shape[:-1]

            if len(batch_info.shape) == 0:
                return arg.unsqueeze(0)

        return arg

    def postprocess(arg, batch_info):
        if type(arg) is torch.Tensor and batch_info.init and len(batch_info.shape) == 0:
            return arg[0, ...]

        return arg

    def wrapper(self, *args, **kwargs):
        batch_info = BatchInfo()

        # Parse input
        processed_args = [preprocess(arg, self, batch_info) for arg in args]
        processed_kwargs = {
            key: preprocess(kwargs[key], self, batch_info) for key in kwargs
        }

        # Perform function
        ret = function(self, *processed_args, **processed_kwargs)

        # Parse output
        if type(ret) is torch.Tensor:
            return postprocess(ret, batch_info)
        elif type(ret) is tuple:
            return tuple([postprocess(r, batch_info) for r in ret])
        else:
            return ret

    return wrapper


class DifferentiableRobotModel(torch.nn.Module):
    """
    Differentiable Robot Model
    ====================================
    TODO
    """

    def __init__(self, urdf_path: str, name="", device=None):

        super().__init__()

        self.name = name

        self._device = (
            torch.device(device) if device is not None else torch.device("cpu")
        )

        self._urdf_model = URDFRobotModel(urdf_path=urdf_path, device=self._device)
        self._bodies = torch.nn.ModuleList()
        self._n_dofs = 0
        self._controlled_joints = []

        # here we're making the joint a part of the rigid body
        # while urdfs model joints and rigid bodies separately
        # joint is at the beginning of a link
        self._name_to_idx_map = dict()

        for (i, link) in enumerate(self._urdf_model.robot.links):
            # Initialize body object
            rigid_body_params = self._urdf_model.get_body_parameters_from_urdf(i, link)
            body = DifferentiableRigidBody(
                rigid_body_params=rigid_body_params, device=self._device
            )

            # Joint properties
            body.joint_idx = None
            if rigid_body_params["joint_type"] != "fixed":
                body.joint_idx = self._n_dofs
                self._n_dofs += 1
                self._controlled_joints.append(i)

            # Add to data structures
            self._bodies.append(body)
            self._name_to_idx_map[body.name] = i

        # Once all bodies are loaded, connect each body to its parent
        for body in self._bodies[1:]:
            parent_body_name = self._urdf_model.get_name_of_parent_body(body.name)
            parent_body_idx = self._name_to_idx_map[parent_body_name]
            body.set_parent(self._bodies[parent_body_idx])
            self._bodies[parent_body_idx].add_child(body)

    @tensor_check
    def update_kinematic_state(self, q: torch.Tensor, qd: torch.Tensor) -> None:
        r"""

        Updates the kinematic state of the robot
        Args:
            q: joint angles [batch_size x n_dofs]
            qd: joint velocities [batch_size x n_dofs]

        Returns:

        """
        assert q.ndim == 2
        assert qd.ndim == 2
        assert q.shape[1] == self._n_dofs
        assert qd.shape[1] == self._n_dofs

        batch_size = q.shape[0]

        # update the state of the joints that are invariant to joint configuration changes to also respect the batch size
        for i in range(self._controlled_joints[0]):
            self._bodies[i].update_joint_state(
                torch.zeros((batch_size, 1), device=self._device),
                torch.zeros((batch_size, 1), device=self._device)
            )

        for i in range(q.shape[1]):
            idx = self._controlled_joints[i]
            self._bodies[idx].update_joint_state(
                q[:, i].unsqueeze(1), qd[:, i].unsqueeze(1)
            )

        # we assume a non-moving base
        parent_body = self._bodies[0]
        parent_body.vel = SpatialMotionVec(
            torch.zeros((batch_size, 3), device=self._device),
            torch.zeros((batch_size, 3), device=self._device),
        )

        # propagate the new joint state through the kinematic chain to update bodies position/velocities
        for i in range(1, len(self._bodies)):

            body = self._bodies[i]
            parent_name = self._urdf_model.get_name_of_parent_body(body.name)
            # find the joint that has this link as child
            parent_body = self._bodies[self._name_to_idx_map[parent_name]]

            # transformation operator from child link to parent link
            childToParentT = body.joint_pose

            # transformation operator from parent link to child link
            parentToChildT = childToParentT.inverse()

            # the position and orientation of the body in world coordinates, with origin at the joint
            body.pose = parent_body.pose.multiply_transform(childToParentT)

            # we rotate the velocity of the parent's body into the child frame
            new_vel = parent_body.vel.transform(parentToChildT)

            # this body's angular velocity is combination of the velocity experienced at it's parent's link
            # + the velocity created by this body's joint
            body.vel = body.joint_vel.add_motion_vec(new_vel)

        return

    @tensor_check
    def compute_forward_kinematics_all_links(
        self, q: torch.Tensor, recursive: bool = False
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""

        Args:
            q: joint angles [batch_size x n_dofs]
            link_name: name of link

        Returns: translation and rotation of the link frame

        """
        if recursive: 
            # Create joint state dictionary
            q_dict = {}
            for i, body_idx in enumerate(self._controlled_joints):
                q_dict[self._bodies[body_idx].name] = q[:, i].unsqueeze(1)

            # Call forward kinematics on root node
            pose_dict = self._bodies[0].forward_kinematics(q_dict)

        else:
            qd = torch.zeros_like(q)
            self.update_kinematic_state(q, qd)

            pose_dict = {}
            for link_name in self.get_link_names():
                pose_dict[link_name] = self._bodies[self._name_to_idx_map[link_name]].pose

        result = {
            link: (pose_dict[link].translation(), pose_dict[link].get_quaternion())
            for link in pose_dict.keys()
        }
        return result

    @tensor_check
    def compute_forward_kinematics(
        self, q: torch.Tensor, link_name: str, recursive: bool = False
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""

        Args:
            q: joint angles [batch_size x n_dofs]
            link_name: name of link

        Returns: translation and rotation of the link frame

        """
        assert q.ndim == 2

        if recursive:
            return self.compute_forward_kinematics_all_links(q)[link_name]

        else:
            qd = torch.zeros_like(q)
            self.update_kinematic_state(q, qd)

            pose = self._bodies[self._name_to_idx_map[link_name]].pose
            pos = pose.translation()
            rot = pose.get_quaternion()
            return pos, rot

    @tensor_check
    def iterative_newton_euler(self, base_acc: SpatialMotionVec) -> None:
        r"""

        Args:
            base_acc: spatial acceleration of base (for fixed manipulators this is zero)
        """

        body = self._bodies[0]
        body.acc = base_acc

        # forward pass to propagate accelerations from root to end-effector link
        for i in range(1, len(self._bodies)):
            body = self._bodies[i]
            parent_name = self._urdf_model.get_name_of_parent_body(body.name)

            parent_body = self._bodies[self._name_to_idx_map[parent_name]]

            # get the inverse of the current joint pose
            inv_pose = body.joint_pose.inverse()

            # transform spatial acceleration of parent body into this body's frame
            acc_parent_body = parent_body.acc.transform(inv_pose)
            # body velocity cross joint vel
            tmp = body.vel.cross_motion_vec(body.joint_vel)
            body.acc = acc_parent_body.add_motion_vec(body.joint_acc).add_motion_vec(
                tmp
            )

        # reset all forces for backward pass
        for i in range(0, len(self._bodies)):
            self._bodies[i].force = SpatialForceVec(device=self._device)

        # backward pass to propagate forces up (from endeffector to root body)
        for i in range(len(self._bodies) - 1, 0, -1):
            body = self._bodies[i]
            joint_pose = body.joint_pose

            # body force on joint
            icxacc = body.inertia.multiply_motion_vec(body.acc)
            icxvel = body.inertia.multiply_motion_vec(body.vel)
            tmp_force = body.vel.cross_force_vec(icxvel)

            body.force = body.force.add_force_vec(icxacc).add_force_vec(tmp_force)

            # pose x body_force => propagate to parent
            if i > 0:
                parent_name = self._urdf_model.get_name_of_parent_body(body.name)
                parent_body = self._bodies[self._name_to_idx_map[parent_name]]

                backprop_force = body.force.transform(joint_pose)
                parent_body.force = parent_body.force.add_force_vec(backprop_force)

        return

    @tensor_check
    def compute_inverse_dynamics(
        self,
        q: torch.Tensor,
        qd: torch.Tensor,
        qdd_des: torch.Tensor,
        include_gravity: Optional[bool] = True,
        use_damping: Optional[bool] = True,
    ) -> torch.Tensor:
        r"""

        Args:
            q: joint angles [batch_size x n_dofs]
            qd: joint velocities [batch_size x n_dofs]
            qdd_des: desired joint accelerations [batch_size x n_dofs]
            include_gravity: when False, we assume gravity compensation is already taken care off

        Returns: forces to achieve desired accelerations

        """
        assert q.ndim == 2
        assert qd.ndim == 2
        assert qdd_des.ndim == 2
        assert q.shape[1] == self._n_dofs
        assert qd.shape[1] == self._n_dofs
        assert qdd_des.shape[1] == self._n_dofs

        batch_size = qdd_des.shape[0]
        force = torch.zeros_like(qdd_des)

        # we set the current state of the robot
        self.update_kinematic_state(q, qd)

        # we set the acceleration of all controlled joints to the desired accelerations
        for i in range(self._n_dofs):
            idx = self._controlled_joints[i]
            self._bodies[idx].update_joint_acc(qdd_des[:, i].unsqueeze(1))

        # forces at the base are either 0, or gravity
        base_ang_acc = q.new_zeros((batch_size, 3))
        base_lin_acc = q.new_zeros((batch_size, 3))
        if include_gravity:
            base_lin_acc[:, 2] = 9.81 * torch.ones(batch_size, device=self._device)

        # we propagate the base forces
        self.iterative_newton_euler(SpatialMotionVec(base_lin_acc, base_ang_acc))

        # we extract the relevant forces for all controlled joints
        for i in range(qdd_des.shape[1]):
            idx = self._controlled_joints[i]
            rot_axis = torch.zeros((batch_size, 3), device=self._device)
            axis = self._bodies[idx].joint_axis[0]
            axis_idx = int(torch.where(axis)[0])
            rot_sign = torch.sign(axis[axis_idx])

            rot_axis[:, axis_idx] = rot_sign * torch.ones(
                batch_size, device=self._device
            )
            force[:, i] += (
                self._bodies[idx].force.ang.unsqueeze(1) @ rot_axis.unsqueeze(2)
            ).squeeze()

        # we add forces to counteract damping
        if use_damping:
            damping_const = torch.zeros((1, self._n_dofs), device=self._device)
            for i in range(self._n_dofs):
                idx = self._controlled_joints[i]
                damping_const[:, i] = self._bodies[idx].get_joint_damping_const()
            force += damping_const.repeat(batch_size, 1) * qd

        return force

    def __euler_from_quaternion(self, quat: torch.Tensor) -> torch.Tensor:
        _EPS = torch.finfo(float).eps * 4.0
        quat = quat[[3, 0, 1, 2]] # from [x, y, z, w] to [w, x, y, z]

        q = quat
        n = quat @ quat
        if n < _EPS:
            return torch.identity(4)
        q = q * torch.sqrt(2.0 / n)
        q = torch.outer(q, q)

        # required to retrain grad
        M = torch.eye(3, 3, device=quat.device)
        M[0, 0] = 1.0 - q[2, 2] - q[3, 3]
        M[0, 1] = q[1, 2] - q[3, 0]
        M[0, 2] = q[1, 3] + q[2, 0]

        M[1, 0] = q[1, 2] + q[3, 0]
        M[1, 1] = 1.0 - q[1, 1] - q[3, 3]
        M[1, 2] = q[2, 3] - q[1, 0]

        M[2, 0] = q[1, 3] - q[2, 0]
        M[2, 1] = q[2, 3] + q[1, 0]
        M[2, 2] = 1.0 - q[1, 1] - q[2, 2]

        cy = torch.sqrt(M[0, 0] * M[0, 0] + M[1, 0] * M[1, 0])
        if cy > _EPS:
            ax = torch.atan2(M[2, 1], M[2, 2])
            ay = torch.atan2(-M[2, 0], cy)
            az = torch.atan2(M[1, 0], M[0, 0])
        else:
            ax = torch.atan2(-M[1, 2], M[1, 1])
            ay = torch.atan2(-M[2, 0], cy)
            az = torch.zeros(1)

        return torch.stack((ax, ay, az))

    def compute_inverse_kinematics_jac(
        self,
        trans: torch.Tensor,
        rot: torch.Tensor,
        link_name: str,
        init_conf: torch.Tensor,
        max_num_iter: int = 1000,
        min_precision: float = 0.1,
        min_convergency_update: float = 1e-3,
        learning_rate: float = 0.1,
        damping_factor: float = 0.04,
        verbose: bool = False
    ) -> torch.Tensor:
        r"""

        Args:
            trans: translation vector [batch_size x 3]
            rot: rotation vector [batch_size x (x, y, z, w)]
            link_name: name of link
            init_conf: initinal configuration [n_dofs] (optional)
            max_num_iter: maximal number of iterations
            min_precision: which precision is good enough
            verbose: show debug log

        Returns: final configuration 
        
        """
        assert trans.ndim == 2, rot.ndim == 2
        assert trans.shape[0] == rot.shape[0]
        assert trans.shape[1] == 3 and rot.shape[1] == 4        
        assert init_conf is not None and len(init_conf) == self._n_dofs

        batch_size = trans.shape[0]
        final_conf = torch.empty((batch_size, self._n_dofs), device=trans.device)

        delta_confs = []

        for th_idx in range(batch_size):
            if th_idx == 0:
                    curr_conf = init_conf
            else:
                curr_conf = final_conf[th_idx-1]

            # must use euler because jacobian only works with euler 
            # tmp = self.__euler_from_quaternion(rot[th_idx])
            # tmp2 = quaternion_to_euler_xyz(rot[None, th_idx]).squeeze()
            # assert torch.allclose(tmp, tmp2)

            # TODO: tested quaternion_to_euler_xyz, but different results. Check why?
            goal_pose = torch.cat((trans[th_idx], self.__euler_from_quaternion(rot[th_idx])))
            min_error = torch.inf

            # logging
            if verbose:
                positions = []
                errors = []
                from tqdm import tqdm
                bar = tqdm(max_num_iter)

            i = 0
            while True:
                curr_pos, curr_rot = self.compute_forward_kinematics(curr_conf, link_name=link_name)
                curr_pose = torch.cat((curr_pos, self.__euler_from_quaternion(curr_rot)))


                curr_delta_pose = goal_pose - curr_pose
                delta_norm = torch.norm(curr_delta_pose)

                if verbose:
                    positions.append(curr_pos.cpu().detach().numpy())
                    errors.append(curr_delta_pose)
                    bar.set_description(f"{th_idx} link loss in {i}: {delta_norm}")

                if delta_norm < min_error:
                    min_error = delta_norm
                    final_conf[th_idx] = curr_conf

                if delta_norm < min_precision:
                    if verbose:
                        logger.debug("min precision reached")
                    break

                if i >= max_num_iter:
                    if verbose:
                        logger.debug("maximum number of iterations reached")
                    break

                lin_jac, ang_jac = self.compute_endeffector_jacobian(curr_conf, link_name=link_name)
                jac = torch.concat((lin_jac, ang_jac), dim=0)

                #### pseudo inverse
                # pjac = torch.linalg.pinv(jac) # pseudo inverse
                # curr_delta_conf = pjac @ curr_delta_pose

                #### damped least squares
                f = torch.linalg.solve(jac @ jac.T + damping_factor ** 2 * torch.eye(len(jac), device=jac.device), curr_delta_pose)
                curr_delta_conf = jac.T @ f
                delta_confs.append(curr_delta_conf)

                if torch.norm(curr_delta_conf) < min_convergency_update:
                    if verbose:
                        logger.debug("convergency update too small")
                    break

                curr_conf = curr_conf + learning_rate * curr_delta_conf

                i += 1

            if verbose:
                logger.debug(f"{th_idx} final loss: {min_error}")

                from matplotlib import pyplot as plt
                from matplotlib import colors
                import numpy as np
                plt.figure()
                plt.title("DLG IK: cartesian error over iteration steps")
                plt.plot(torch.stack(errors).cpu().detach().numpy(), label=["x", "y", "z", "ax", "xy", "xz"])
                plt.yscale('log')
                plt.legend()
                plt.show()

                fig = plt.figure()
                plt.title("Manipulator Pose in cartesian space over iteration steps")
                cmap = colors.LinearSegmentedColormap.from_list("", ["green","red"]) 
                ax = fig.add_subplot(111, projection='3d')
                # ax.plot(*np.array(positions).T, c='blue')
                ax.scatter(*np.array(positions).T, c=[cmap(x/len(positions)) for x in range(len(positions))])
                ax.scatter(*goal_pose[:3].cpu().detach(), c='black')
                ax.scatter(*positions[0], c='green')
                plt.show()


        if min_error > min_precision:
            logger.debug(f"differentiable ik accuracy above min precision {min_precision}: {min_error}")

        return final_conf

    @tensor_check
    def compute_inverse_kinematics_gb(
        self,
        trans: torch.Tensor,
        rot: torch.Tensor,
        link_name: str,
        init_conf: Optional[torch.Tensor] = None,
        max_num_iter: int = 1000,
        min_precision: float = 0.1,
        learning_rate: float = 0.1,
        verbose: bool = False
    ) -> torch.Tensor:
        r"""

        Args:
            trans: translation vector [batch_size x 3]
            rot: rotation vector [batch_size x 4]
            link_name: name of link
            init_conf: initinal configuration for first batch_size variable [n_dofs] (optional)
            max_num_iter: maximal number of iterations
            min_precision: which precision is good enough
            verbose: show debug log

        Returns: final configuration 
        
        """
        assert trans.ndim == 2, rot.ndim == 2
        assert trans.shape[0] == rot.shape[0]
        assert trans.shape[1] == 3 and rot.shape[1] == 4

        batch_size = trans.shape[0]
        final_conf = torch.empty((batch_size, self._n_dofs))

        for th_idx in range(batch_size):
            if th_idx == 0:
                if init_conf is None:
                    curr_conf = torch.zeros(self._n_dofs) # TODO: find better approximation?!
                else:
                    curr_conf = init_conf
            else:
                curr_conf = torch.clone(final_conf[th_idx-1]).detach()

            curr_conf.requires_grad = True        

            optimizer = torch.optim.Adam([curr_conf], lr=learning_rate)
            lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=100, cooldown=10,verbose=True)
            lss_fn = lambda x, y: torch.norm(x - y, dim=-1)

            goal_pose = torch.cat((trans[th_idx], rot[th_idx]), dim=-1)

            for i in range(max_num_iter):
                optimizer.zero_grad()
                curr_pos, curr_rot = self.compute_forward_kinematics(curr_conf, link_name=link_name)
                curr_pose = torch.cat((curr_pos, curr_rot), dim=-1)

                loss = lss_fn(curr_pose, goal_pose)
                total_loss = torch.sum(loss)
                total_loss.backward()
                optimizer.step()
                lr_scheduler.step(total_loss)

                if verbose:
                    logger.debug(f"total loss for thx {th_idx} in epoch {i}: {total_loss} m")

                if total_loss < min_precision:
                    break

            final_conf[th_idx] = curr_conf

        return final_conf


    @tensor_check
    def compute_non_linear_effects(
        self,
        q: torch.Tensor,
        qd: torch.Tensor,
        include_gravity: Optional[bool] = True,
        use_damping: Optional[bool] = True,
    ) -> torch.Tensor:
        r"""

        Compute the non-linear effects (Coriolis, centrifugal, gravitational, and damping effects).

        Args:
            q: joint angles [batch_size x n_dofs]
            qd: [batch_size x n_dofs]
            include_gravity: set to False if your robot has gravity compensation

        Returns:

        """
        zero_qdd = q.new_zeros(q.shape)
        return self.compute_inverse_dynamics(
            q, qd, zero_qdd, include_gravity, use_damping
        )

    @tensor_check
    def compute_lagrangian_inertia_matrix(
        self,
        q: torch.Tensor,
        include_gravity: Optional[bool] = True,
        use_damping: Optional[bool] = True,
    ) -> torch.Tensor:
        r"""

        Args:
            q: joint angles [batch_size x n_dofs]
            include_gravity: set to False if your robot has gravity compensation

        Returns:

        """
        assert q.shape[1] == self._n_dofs
        batch_size = q.shape[0]
        identity_tensor = (
            torch.eye(q.shape[1], device=self._device)
            .unsqueeze(0)
            .repeat(batch_size, 1, 1)
        )
        zero_qd = q.new_zeros(q.shape)
        zero_qdd = q.new_zeros(q.shape)
        if include_gravity:
            gravity_term = self.compute_inverse_dynamics(
                q, zero_qd, zero_qdd, include_gravity, use_damping
            )
        else:
            gravity_term = q.new_zeros(q.shape)

        H = torch.stack(
            [
                (
                    self.compute_inverse_dynamics(
                        q,
                        zero_qd,
                        identity_tensor[:, :, j],
                        include_gravity,
                        use_damping,
                    )
                    - gravity_term
                )
                for j in range(self._n_dofs)
            ],
            dim=2,
        )
        return H

    @tensor_check
    def compute_forward_dynamics_old(
        self,
        q: torch.Tensor,
        qd: torch.Tensor,
        f: torch.Tensor,
        include_gravity: Optional[bool] = True,
        use_damping: Optional[bool] = True,
    ) -> torch.Tensor:
        r"""
        Computes next qdd by solving the Euler-Lagrange equation
        qdd = H^{-1} (F - Cv - G - damping_term)

        Args:
            q: joint angles [batch_size x n_dofs]
            qd: joint velocities [batch_size x n_dofs]
            f: forces to be applied [batch_size x n_dofs]
            include_gravity: set to False if your robot has gravity compensation

        Returns: accelerations that are the result of applying forces f in state q, qd

        """

        nle = self.compute_non_linear_effects(
            q=q, qd=qd, include_gravity=include_gravity, use_damping=use_damping
        )
        inertia_mat = self.compute_lagrangian_inertia_matrix(
            q=q, include_gravity=include_gravity, use_damping=use_damping
        )

        # Solve H qdd = F - Cv - G - damping_term
        qdd = torch.solve(f.unsqueeze(2) - nle.unsqueeze(2), inertia_mat)[0].squeeze(2)

        return qdd

    @tensor_check
    def compute_forward_dynamics(
        self,
        q: torch.Tensor,
        qd: torch.Tensor,
        f: torch.Tensor,
        include_gravity: Optional[bool] = True,
        use_damping: Optional[bool] = False,
    ) -> torch.Tensor:
        r"""
        Computes next qdd via the articulated body algorithm (see Featherstones Rigid body dynamics page 132)

        Args:
            q: joint angles [batch_size x n_dofs]
            qd: joint velocities [batch_size x n_dofs]
            f: forces to be applied [batch_size x n_dofs]
            include_gravity: set to False if your robot has gravity compensation

        Returns: accelerations that are the result of applying forces f in state q, qd

        """
        assert q.ndim == 2
        assert qd.ndim == 2
        assert q.shape[1] == self._n_dofs
        assert qd.shape[1] == self._n_dofs

        qdd = torch.zeros_like(q)
        batch_size = q.shape[0]

        if use_damping:
            damping_const = torch.zeros((1, self._n_dofs), device=self._device)
            for i in range(self._n_dofs):
                idx = self._controlled_joints[i]
                damping_const[:, i] = self._bodies[idx].get_joint_damping_const()
            f -= damping_const.repeat(batch_size, 1) * qd

        # we set the current state of the robot
        self.update_kinematic_state(q, qd)

        # forces at the base are either 0, or gravity
        base_ang_acc = q.new_zeros((batch_size, 3))
        base_lin_acc = q.new_zeros((batch_size, 3))
        if include_gravity:
            base_lin_acc[:, 2] = 9.81 * torch.ones(batch_size, device=self._device)

        base_acc = SpatialMotionVec(base_lin_acc, base_ang_acc)

        body = self._bodies[0]
        body.acc = base_acc

        for i in range(1, len(self._bodies)):
            body = self._bodies[i]

            # body velocity cross joint vel
            body.c = body.vel.cross_motion_vec(body.joint_vel)
            icxvel = body.inertia.multiply_motion_vec(body.vel)
            body.pA = body.vel.cross_force_vec(icxvel)
            # IA is 6x6, we repeat it for each item in the batch, as the raw inertia matrix is shared across the whole batch
            body.IA = body.inertia.get_spatial_mat().repeat((batch_size, 1, 1))

        for i in range(len(self._bodies) - 1, 0, -1):
            body = self._bodies[i]

            S = SpatialMotionVec(
                lin_motion=torch.zeros((batch_size, 3), device=self._device),
                ang_motion=body.joint_axis.repeat((batch_size, 1)),
            )
            body.S = S
            Utmp = torch.bmm(body.IA, S.get_vector()[..., None])[..., 0]
            body.U = SpatialForceVec(lin_force=Utmp[:, 3:], ang_force=Utmp[:, :3])
            body.d = S.dot(body.U)
            if body.joint_idx is not None:
                body.u = f[:, body.joint_idx] - body.pA.dot(S)
            else:
                body.u = -body.pA.dot(S)

            parent_name = self._urdf_model.get_name_of_parent_body(body.name)
            parent_idx = self._name_to_idx_map[parent_name]

            if parent_idx > 0:
                parent_body = self._bodies[parent_idx]
                U = body.U.get_vector()
                Ud = U / (
                    body.d.view(batch_size, 1) + 1e-37
                )  # add smoothing values in case of zero mass
                c = body.c.get_vector()

                # IA is of size [batch_size x 6 x 6]
                IA = body.IA - torch.bmm(
                    U.view(batch_size, 6, 1), Ud.view(batch_size, 1, 6)
                )

                tmp = torch.bmm(IA, c.view(batch_size, 6, 1)).squeeze(dim=2)
                tmps = SpatialForceVec(lin_force=tmp[:, 3:], ang_force=tmp[:, :3])
                ud = body.u / (
                    body.d + 1e-37
                )  # add smoothing values in case of zero mass
                uu = body.U.multiply(ud)
                pa = body.pA.add_force_vec(tmps).add_force_vec(uu)

                joint_pose = body.joint_pose

                # transform is of shape 6x6
                transform_mat = joint_pose.to_matrix()
                if transform_mat.shape[0] != IA.shape[0]:
                    transform_mat = transform_mat.repeat(IA.shape[0], 1, 1)
                parent_body.IA += torch.bmm(transform_mat.transpose(-2, -1), IA).bmm(
                    transform_mat
                )
                parent_body.pA = parent_body.pA.add_force_vec(pa.transform(joint_pose))

        base_acc = SpatialMotionVec(lin_motion=base_lin_acc, ang_motion=base_ang_acc)

        body = self._bodies[0]
        body.acc = base_acc

        # forward pass to propagate accelerations from root to end-effector link
        for i in range(1, len(self._bodies)):
            body = self._bodies[i]
            parent_name = self._urdf_model.get_name_of_parent_body(body.name)
            parent_idx = self._name_to_idx_map[parent_name]
            parent_body = self._bodies[parent_idx]

            # get the inverse of the current joint pose
            inv_pose = body.joint_pose.inverse()

            # transform spatial acceleration of parent body into this body's frame
            acc_parent_body = parent_body.acc.transform(inv_pose)
            # body velocity cross joint vel
            body.acc = acc_parent_body.add_motion_vec(body.c)

            # Joint acc
            if i in self._controlled_joints:
                joint_idx = self._controlled_joints.index(i)
                qdd[:, joint_idx] = (1.0 / body.d) * (body.u - body.U.dot(body.acc))
                body.acc = body.acc.add_motion_vec(body.S.multiply(qdd[:, joint_idx]))

        return qdd

    @tensor_check
    def compute_endeffector_jacobian(
        self, q: torch.Tensor, link_name: str
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""

        Args:
            link_name: name of link name for the jacobian
            q: joint angles [batch_size x n_dofs]

        Returns: linear and angular jacobian

        """
        assert len(q.shape) == 2
        batch_size = q.shape[0]
        self.compute_forward_kinematics(q, link_name)

        e_pose = self._bodies[self._name_to_idx_map[link_name]].pose
        p_e = e_pose.translation()

        lin_jac, ang_jac = (
            torch.zeros([batch_size, 3, self._n_dofs], device=self._device),
            torch.zeros([batch_size, 3, self._n_dofs], device=self._device),
        )

        joint_id = self._bodies[self._name_to_idx_map[link_name]].joint_id
        while link_name != self._bodies[0].name:
            if joint_id in self._controlled_joints:
                i = self._controlled_joints.index(joint_id)
                idx = joint_id

                pose = self._bodies[idx].pose
                axis = self._bodies[idx].joint_axis
                p_i = pose.translation()
                z_i = pose.rotation() @ axis.squeeze()
                lin_jac[:, :, i] = torch.cross(z_i, p_e - p_i, dim=-1)
                ang_jac[:, :, i] = z_i

            link_name = self._urdf_model.get_name_of_parent_body(link_name)
            joint_id = self._bodies[self._name_to_idx_map[link_name]].joint_id

        return lin_jac, ang_jac

    @tensor_check
    def compute_endeffector_jacobian_all_links(
        self, q: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""

        Args:
            q: joint angles [batch_size x n_dofs]

        Returns: linear and angular jacobian of all links

        """
        assert len(q.shape) == 2
        link_names = self.get_link_names()
        link_name = link_names[-1]
        num_links = len(link_names)

        batch_size = q.shape[0]
        self.compute_forward_kinematics(q, link_name)

        p_es = [
            self._bodies[self._name_to_idx_map[l]].pose.translation()
            for l in link_names[1:]
        ]
        p_es = torch.stack(p_es, dim=1)

        lin_jac, ang_jac = (
            torch.zeros([batch_size, num_links, 3, self._n_dofs], device=self._device),
            torch.zeros([batch_size, num_links, 3, self._n_dofs], device=self._device),
        )

        joint_id = self._bodies[self._name_to_idx_map[link_name]].joint_id
        while link_name != self._bodies[0].name:
            if joint_id in self._controlled_joints:
                i = self._controlled_joints.index(joint_id)
                idx = joint_id

                pose = self._bodies[idx].pose
                axis = self._bodies[idx].joint_axis
                p_i = pose.translation()
                z_i = pose.rotation() @ axis.squeeze()
                lin_jac[:, idx+1:, :, i] = torch.cross(z_i[:, None], p_es[:, idx:] - p_i[:, None], dim=-1)
                ang_jac[:, idx:, :, i] = z_i[:, None]

            link_name = self._urdf_model.get_name_of_parent_body(link_name)
            joint_id = self._bodies[self._name_to_idx_map[link_name]].joint_id

        return lin_jac, ang_jac

    def _get_parent_object_of_param(self, link_name: str, parameter_name: str):
        body_idx = self._name_to_idx_map[link_name]
        if parameter_name in ["trans", "rot_angles", "joint_damping"]:
            parent_object = self._bodies[body_idx]
        elif parameter_name in ["mass", "inertia_mat", "com"]:
            parent_object = self._bodies[body_idx].inertia
        else:
            raise AttributeError(
                "Invalid parameter name. Accepted parameter names are: "
                "trans, rot_angles, joint_damping, mass, inertia_mat, com"
            )
        return parent_object

    def make_link_param_learnable(
        self, link_name: str, parameter_name: str, parametrization: torch.nn.Module
    ):
        parent_object = self._get_parent_object_of_param(link_name, parameter_name)

        # Replace current parameter with a learnable module
        parent_object.__delattr__(parameter_name)
        parent_object.add_module(parameter_name, parametrization.to(self._device))

    def freeze_learnable_link_param(self, link_name: str, parameter_name: str):
        parent_object = self._get_parent_object_of_param(link_name, parameter_name)

        # Get output value of current module
        param_module = getattr(parent_object, parameter_name)
        assert (
            type(param_module).__bases__[0] is torch.nn.Module
        ), f"{parameter_name} of {link_name} is not a learnable module."

        for param in param_module.parameters():
            param.requires_grad = False

    def unfreeze_learnable_link_param(self, link_name: str, parameter_name: str):
        parent_object = self._get_parent_object_of_param(link_name, parameter_name)

        # Get output value of current module
        param_module = getattr(parent_object, parameter_name)
        assert (
            type(param_module).__bases__[0] is torch.nn.Module
        ), f"{parameter_name} of {link_name} is not a learnable module."

        for param in param_module.parameters():
            param.requires_grad = True

    def get_joint_limits(self) -> List[Dict[str, torch.Tensor]]:
        r"""

        Returns: list of joint limit dict, containing joint position, velocity and effort limits

        """
        limits = []
        for idx in self._controlled_joints:
            limits.append(self._bodies[idx].get_joint_limits())
        return limits

    def get_link_names(self) -> List[str]:
        r"""

        Returns: a list containing names for all links

        """

        link_names = []
        for i in range(len(self._bodies)):
            link_names.append(self._bodies[i].name)
        return link_names

    def print_link_names(self) -> None:
        r"""

        print the names of all links

        """
        for i in range(len(self._bodies)):
            logger.debug(self._bodies[i].name)

    def print_learnable_params(self) -> None:
        r"""

        print the name and value of all learnable parameters

        """
        for name, param in self.named_parameters():
            logger.debug(f"{name}: {param}")
