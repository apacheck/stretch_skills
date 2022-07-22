#!/usr/bin/env python

from __future__ import print_function

from sensor_msgs.msg import JointState
from geometry_msgs.msg import Twist, Quaternion, Transform
from nav_msgs.msg import Odometry

import rospy
import actionlib
from control_msgs.msg import FollowJointTrajectoryAction
from control_msgs.msg import FollowJointTrajectoryGoal
from trajectory_msgs.msg import JointTrajectoryPoint, JointTrajectory
from gazebo_msgs.msg import ModelState
from gazebo_msgs.srv import SetModelState
from tf.transformations import quaternion_from_euler

from visualization_msgs.msg import MarkerArray, Marker

from geometry_msgs.msg import PointStamped

from sensor_msgs.msg import PointCloud2

from std_srvs.srv import Trigger, TriggerRequest, TriggerResponse
from aut_tools import find_intermediate_symbols, find_skill_to_run, find_state_number, update_state, parse_spec, parse_aut, find_symbols
import argparse
from synthesis_based_repair.skills import load_skills_from_json
from synthesis_based_repair.symbols import load_symbols

import math
import time
import threading
import sys

from math import dist

import tf2_ros
import argparse as ap
import numpy as np
import threading
import ros_numpy as rn

import hello_helpers.hello_misc as hm
import moveit_commander
import moveit_msgs.msg
from moveit_commander.conversions import pose_to_list
from synthesis_based_repair.tools import write_spec, clear_file, dict_to_formula, json_load_wrapper

from dl2_lfd.nns.dmp_nn import DMPNN
from dl2_lfd.dmps.dmp import load_dmp_demos, DMP
import torch
from dl2_lfd.helper_funcs.conversions import np_to_pgpu
from gazebo_ros_link_attacher.srv import Attach, AttachRequest, AttachResponse


from StretchHelpers import feedbackLin, thresholdVel, findCommands, findArmExtensionAndRotation, findTheta

DEVICE="cpu"

# import stretch_funmap.navigate as nv

IS_SIM = True
if IS_SIM:
    TELEPORT=True
    ORIGIN_FRAME = 'odom'
    STRETCH_FRAME = 'robot::base_link'
    CMD_VEL_TOPIC = '/stretch_diff_drive_controller/cmd_vel'
    EE_FRAME = "fake_finger"
    DUCK1_FRAME = "duck_1::body"
    DUCK2_FRAME = "duck_2::body"
    DO_THETA_CORRECTION = False
    DO_MOVE_GRIPPER = True
    OBJ_NAME = 'duck_1::body'
else:
    ORIGIN_FRAME = 'origin'
    STRETCH_FRAME = 'stretch'
    CMD_VEL_TOPIC = '/stretch/cmd_vel'
    DO_THETA_CORRECTION = False
    DO_MOVE_GRIPPER = True
    OBJ_NAME = 'Duck'

class StretchSkill(hm.HelloNode):
    def __init__(self):
        rospy.loginfo("Creating stretch skill")
        if IS_SIM:
            moveit_commander.roscpp_initialize(sys.argv)
            rospy.init_node('controller', anonymous=True)
            robot = moveit_commander.RobotCommander()
            # rospy.sleep(10.0)
            self.move_group_arm = moveit_commander.MoveGroupCommander("stretch_arm")
            self.attach_srv = rospy.ServiceProxy('/link_attacher_node/attach', Attach)
            self.attach_srv.wait_for_service()
            self.detach_srv = rospy.ServiceProxy('/link_attacher_node/detach', Attach)
            self.detach_srv.wait_for_service()
            self.teleport_base_srv = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)
            self.teleport_base_srv.wait_for_service()
        else:
            hm.HelloNode.__init__(self)
            hm.HelloNode.main(self, 'stretch_control', 'stretch_skill_repair', wait_for_first_pointcloud=False)
        self.rate = rospy.Rate(20.0)
        self.joint_states = None

        # For use with mobile base control
        self.tfBuffer = tf2_ros.Buffer()
        self.listener = tf2_ros.TransformListener(self.tfBuffer)
        self.vel_pub = rospy.Publisher(CMD_VEL_TOPIC, Twist, queue_size=10)

        if not IS_SIM:
            self.joint_states_lock = threading.Lock()

            self.move_lock = threading.Lock()

            with self.move_lock:
                self.handover_goal_ready = False

    def setStretchFrame(self, stretch_frame):
        self.stretch_frame = stretch_frame

    def setEEFrame(self, ee_frame):
        self.ee_frame = ee_frame

    def setDuck1Frame(self, duck1_frame):
        self.duck1_frame = duck1_frame

    def setDuck2Frame(self, duck2_frame):
        self.duck2_frame = duck2_frame

    def setOriginFrame(self, origin_frame):
        self.origin_frame = origin_frame

    def getWorldState(self):
        """Gets the state of the world

        """

        robot = self.findPose(self.stretch_frame)
        ee = self.findPose(self.ee_frame)
        duck1 = self.findPose(self.duck1_frame)
        duck2 = self.findPose(self.duck2_frame)

        state = np.zeros([1, 12])
        state[0, 0] = robot.translation.x
        state[0, 1] = robot.translation.y
        state[0, 2] = findTheta(robot)
        state[0, 3] = ee.translation.x
        state[0, 4] = ee.translation.y
        state[0, 5] = ee.translation.z
        state[0, 6] = duck1.translation.x
        state[0, 7] = duck1.translation.y
        state[0, 8] = duck1.translation.z
        state[0, 9] = duck2.translation.x
        state[0, 10] = duck2.translation.y
        state[0, 11] = duck2.translation.z

        return state

    def openGripper(self, obj_name=None):
        if DO_MOVE_GRIPPER:
            rospy.loginfo("Opening gripper")
            if not IS_SIM:
                with self.move_lock:
                    pose = {
                    'gripper_aperture': -0.019
                    }
                    self.move_to_pose(pose)
                    rospy.sleep(1)
            else:
                self.detachObject(obj_name)
        else:
            rospy.loginfo("Global parameter set to not move gripper")

    def closeGripper(self, obj_name=None):
        if DO_MOVE_GRIPPER:

            rospy.loginfo("Closing gripper")

            if not IS_SIM:
                with self.move_lock:
                    pose = {
                    'gripper_aperture': -0.05
                    }
                    self.move_to_pose(pose)
                    rospy.sleep(1)
            else:
                self.attachObject(obj_name)
        else:
            rospy.loginfo("Global parameter set to not move gripper")

    def attachObject(self, obj_name):
        rospy.loginfo("Attaching gripper and {}".format(obj_name))
        req = AttachRequest()
        req.model_name_1 = "robot"
        req.link_name_1 = "link_gripper_finger_left"
        req.model_name_2 = obj_name
        req.link_name_2 = "body"

        self.attach_srv.call(req)

    def detachObject(self, obj_name):
        rospy.loginfo("Detaching gripper and {}".format(obj_name))
        req = AttachRequest()
        req.model_name_1 = "robot"
        req.link_name_1 = "link_gripper_finger_left"
        req.model_name_2 = obj_name
        req.link_name_2 = "body"

        self.detach_srv.call(req)

    def findPose(self, frame):
        #TODO Change this to not be a class function, but pass in the transform or something?
        found_transform = False
        cnt = 1
        while not found_transform:
            try:
                trans_stamped = self.tfBuffer.lookup_transform(self.origin_frame, frame, rospy.Time.now(), rospy.Duration(1.0))
                trans = trans_stamped.transform
                found_transform = True
            except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
                self.rate.sleep()
                cnt += 1
                if cnt % 2 == 0:
                    rospy.loginfo("Can't find transform between {} and {}".format(self.origin_frame, frame))
                continue

        return trans

    def getRobotState(self):
        robot = self.findPose(self.stretch_frame)
        joints = self.getJointValues()

        out = np.zeros([1, 6])
        out[0, 0] = robot.translation.x
        out[0, 1] = robot.translation.y
        out[0, 2] = findTheta(robot)
        out[0, 3:] = joints

        return out

    def getJointValues(self):
        # Returns extension, lift, wrist yaw
        if IS_SIM:
            move_group_arm = moveit_commander.MoveGroupCommander("stretch_arm")
            joint_values = move_group_arm.get_current_joint_values()
        else:
            rospy.loginfo("Not implemented")
        return np.array([np.sum(joint_values[1:5]), joint_values[0], joint_values[5]])

    def followTrajectory(self, data, teleport=TELEPORT):
        # Data should be a numpy array with x, y, theta, wrist_extension, z, wrist_theta
        rospy.loginfo("Starting followTrajectory with teleport={}".format(teleport))

        traj_log = np.zeros([data.shape[0], 12])
        for ii, d in enumerate(data):

            self.moveArm(d[3:])

            if d[0] != -10:
                self.visitWaypoint(d[:3], teleport=teleport)

            if DO_THETA_CORRECTION and d[2] != -10 and not teleport:
                self.rotateToTheta(d[2])

            trans_stretch = self.findPose(STRETCH_FRAME)
            theta = findTheta(trans_stretch)

            rospy.loginfo("Robot is at: x: {:.3f}, y: {:.3f}, theta: {:.3f}".format(trans_stretch.translation.x, trans_stretch.translation.y, theta))

            traj_log[ii, :] = self.getWorldState()

        rospy.loginfo("Completed followTrajectory")

        # if IS_SIM:
        #     self.move_group_arm.stop()

        return traj_log

    def rotateToTheta(self, arg_goal_theta, arg_close_enough=0.05):
        make_theta_correction = True
        while make_theta_correction:
            trans_stretch = self.findPose(STRETCH_FRAME)
            theta = findTheta(trans_stretch)
            cmd_w = arg_goal_theta - theta
            if cmd_w > np.pi:
                cmd_w -= 2 * np.pi
            if cmd_w < -np.pi:
                cmd_w += 2 * np.pi
            if np.abs(cmd_w) < arg_close_enough:
                make_theta_correction = False
            else:
                # rospy.loginfo("cmd_v: {} cmd_w: {}".format(0, cmd_w))
                vel_msg = Twist()
                vel_msg.angular.z = cmd_w
                self.vel_pub.publish(vel_msg)
                self.rate.sleep()

        return True

    def visitWaypoint(self, waypoint_xytheta, arg_close_enough=0.03, arg_epsilon=0.1, arg_maxV=2, arg_wheel2center=0.1778, teleport=TELEPORT):
        rospy.loginfo("{} base to: x: {:.2f}, y: {:.2f}, theta: {:.2f}".format("Teleporting" if teleport else "Moving", waypoint_xytheta[0], waypoint_xytheta[1], waypoint_xytheta[2]))

        if teleport:
            self.teleport_base(waypoint_xytheta[0], waypoint_xytheta[1], waypoint_xytheta[2])
            rospy.sleep(0.05)
            return True

        at_waypoint = False
        while not at_waypoint:
            trans_stretch = self.findPose(STRETCH_FRAME)

            dist_to_waypoint = np.sqrt([np.square(waypoint_xytheta[0] - trans_stretch.translation.x) +
                                        np.square(waypoint_xytheta[1] - trans_stretch.translation.y)])
            # rospy.loginfo("Distance to waypoint: {}".format(dist_to_waypoint))

            if dist_to_waypoint < arg_close_enough:
                at_waypoint = True

            if not at_waypoint:
                cmd_vx, cmd_vy, theta = findCommands(trans_stretch, waypoint_xytheta)
                print("cmd_vx: {} cmd_vy: {}".format(cmd_vx, cmd_vy))
                cmd_v, cmd_w = feedbackLin(cmd_vx, cmd_vy, theta, arg_epsilon)
                # cmd_v, cmd_w = thresholdVel(cmd_v, cmd_w, maxV, wheel2center)
                # _, cmd_v, cmd_w = calc_control_command(cmd_vx, cmd_vy, theta, epsilon)

                rospy.loginfo("Robot is at: x: {:.3f}, y: {:.3f}, theta: {:.3f}".format(trans_stretch.translation.x, trans_stretch.translation.y, theta))
                rospy.loginfo("cmd_v: {} cmd_w: {}".format(cmd_v, cmd_w))

                vel_msg = Twist()
                vel_msg.linear.x = cmd_v
                vel_msg.angular.z = cmd_w
                # self.vel_pub.publish(vel_msg)
                self.rate.sleep()

        return True

    def moveArm(self, arg_desired_ext_lift_yaw):
        dl = arg_desired_ext_lift_yaw.tolist()
        rospy.loginfo("Moving gripper and arm to: extension: {:.2f}, lift: {:.2f}, rotation: {:.2f}".format(arg_desired_ext_lift_yaw[0], arg_desired_ext_lift_yaw[1], arg_desired_ext_lift_yaw[2]))
        if not IS_SIM:
            with self.move_lock:
                pose = dict()
                if arg_desired_ext_lift_yaw[0] != -10:
                    pose['wrist_extension'] = arg_desired_ext_lift_yaw[0]
                if arg_desired_ext_lift_yaw[1] != -10:
                    pose['joint_lift'] = arg_desired_ext_lift_yaw[1]
                if arg_desired_ext_lift_yaw[2] != -10:
                    pose['joint_wrist_yaw'] = arg_desired_ext_lift_yaw[2]
                # 'rotate_mobile_base': 0.1
                # 'translate_mobile_base': -0.2

                # The base can also be controlled with odometery?
                # translate_mobile_base
                # rotate_mobile_base
                self.move_to_pose(pose)
        else:
            joint_goal = self.move_group_arm.get_current_joint_values()
            if dl[1] != -10:
                if dl[1] < 0:
                    dl[1] = 0
                if dl[1] > 1:
                    dl[1] = 1
                joint_goal[0] = dl[1]
            if dl[0] != -10:
                if dl[0] < 0:
                    dl[0] = 0
                if dl[0] > 0.5:
                    dl[0] = 0.5
                joint_goal[1] = dl[0]/4
                joint_goal[2] = dl[0]/4
                joint_goal[3] = dl[0]/4
                joint_goal[4] = dl[0]/4
            if dl[2] != -10:
                joint_goal[5] = dl[2]
            # rospy.loginfo("Joint goal {}".format(joint_goal))
            self.move_group_arm.go(joint_goal, wait=True)

        return True


    def run_skill(self, skill_name, inp_state, inp_robot, sym_state, skills, symbols, dmp_folder, opts, teleport=TELEPORT):
        """
        """
        base_skill = skill_name.split("_")[0]
        end_robot = skills[skill_name].get_final_robot_pose(inp_robot, inp_state, symbols)
        print("Goal robot pose: {}".format(end_robot))
        split_skill_name = skill_name.split("_")[0]
        traj = findTrajectoryFromDMP(inp_robot, end_robot, split_skill_name, dmp_folder, opts)

        if base_skill in ['skillStretch3to1', 'skillStretch1to2', 'skillStretch2to3']:
            intermediate_states = self.followTrajectory(traj, teleport=teleport)
        elif base_skill in ['skillStretchDownUp1', 'skillStretchDownUp2', 'skillStretchDownUp3']:
            # n_waypoints = int(traj.shape[0] / 2)
            # first_half = self.followTrajectory(traj[:n_waypoints, :])
            # syms = skills[skill_name].get_ee_final_symbol()
            # print("Symbols in final state: ", syms)
            # if "duck_a_" + base_skill[-1] in syms:
            #     duck = 'duck_1'
            # else:
            #     duck = 'duck_2'
            # if 'place' in skill_name:
            #     self.openGripper(duck)
            # elif 'pickup' in skill_name:
            #     self.closeGripper(duck)
            # second_half = self.followTrajectory(traj[n_waypoints:, :])
            # intermediate_states = np.vstack([first_half, second_half])
            syms = skills[skill_name].get_ee_final_symbol()
            if "duck_a_" + base_skill[-1] in syms:
                duck = 'duck_1'
            else:
                duck = 'duck_2'

            duck_pose = self.findPose(duck + "::body")
            lift = duck_pose.translation.z + 0.07
            robot_pose = Transform()
            robot_pose.translation.x = inp_robot[0, 0]
            robot_pose.translation.y = inp_robot[0, 1]
            robot_pose.rotation = Quaternion(*quaternion_from_euler(0, 0, inp_robot[0, 2]))
            # ext, yaw = findArmExtensionAndRotation(duck_pose, robot_pose)
            yaw = 0
            ext = dist((duck_pose.translation.x, duck_pose.translation.y), (robot_pose.translation.x, robot_pose.translation.y)) - (0.36)
            intermediate_states = np.zeros([3, inp_state.shape[1]])
            intermediate_states[0, :] = self.getWorldState()
            self.moveArm(np.array([ext, lift, yaw]))
            intermediate_states[1, :] = self.getWorldState()
            if 'place' in skill_name:
                self.openGripper(duck)
            elif 'pickup' in skill_name:
                self.closeGripper(duck)
            self.moveArm(np.array([-10, lift+0.2, -10]))
            intermediate_states[2, :] = self.getWorldState()

        return intermediate_states

    def teleport_base(self, robot_x, robot_y, robot_theta):
        """Teleports the robot to the desired pose

        Args:
            robot_x
            robot_y
            robot_theta: radians

        """
        # rospy.loginfo("Teleporting to x: {:.3f} y: {:.3f} theta: {:.3f}".format(robot_x, robot_y, robot_theta))
        ms_msg = ModelState()
        ms_msg.model_name = 'robot'
        ms_msg.pose.position.x = robot_x
        ms_msg.pose.position.y = robot_y
        ms_msg.pose.orientation = Quaternion(*quaternion_from_euler(0, 0, robot_theta))
        self.teleport_base_srv.call(ms_msg)


def findTrajectoryFromDMP(start_pose, end_pose, skill_name, dmp_folder, opts):
    model = DMPNN(opts['start_dimension'], 1024, opts['dimension'], opts['basis_fs']).to(DEVICE)
    model.load_state_dict(torch.load(dmp_folder + skill_name + ".pt"))
    starts = np.zeros([1, 2, start_pose.size])
    starts[0, 0, :] = start_pose
    starts[0, 1, :] = end_pose
    rospy.loginfo("Starts {}".format(starts))
    learned_weights = model(np_to_pgpu(starts))
    dmp = DMP(opts['basis_fs'], opts['dt'], opts['dimension'])
    # print("Calculating rollout")
    learned_rollouts, _, _ = \
        dmp.rollout_torch(torch.tensor(starts[:, 0, :]).to(DEVICE), torch.tensor(starts[:, 1, :]).to(DEVICE), learned_weights)
    out = learned_rollouts[0, :, :].cpu().detach().numpy()

    out = np.vstack([out, end_pose])
    rospy.loginfo("Learned rollout {}".format(out))

    return out


def findObjectPickupPose(obj_pose, obj_name):
    stretch_pose = -10 * np.ones([6])
    if obj_name == "box_1::base_link":
        stretch_pose[0] = obj_pose.translation.x - 0.04
        stretch_pose[1] = obj_pose.translation.y - 0.75
        stretch_pose[2] = np.pi
    elif obj_name == "box_2::base_link":
        stretch_pose[0] = obj_pose.translation.x - 0.75
        stretch_pose[1] = obj_pose.translation.y - 0.04
        stretch_pose[2] = 3 * np.pi / 2
    elif obj_name == "box_3::base_link":
        stretch_pose[0] = obj_pose.translation.x + 0.04
        stretch_pose[1] = obj_pose.translation.y + 0.75
        stretch_pose[2] = 0
    elif obj_name == 'duck_1::body':
        stretch_pose[0] = obj_pose.translation.x - 0.04
        stretch_pose[1] = obj_pose.translation.y - 0.75
        stretch_pose[2] = np.pi

    return stretch_pose


def addJointValuesToPose(arg_stretch_pose, arg_joint_values):
    arg_stretch_pose[3] = arg_joint_values[0]
    arg_stretch_pose[4] = arg_joint_values[1]
    arg_stretch_pose[5] = arg_joint_values[2]

    return arg_stretch_pose


def plotTrajectory(arg_trajectory):
    """Plots the trajectory of the stretch moving the base and arm
    """
    pass


def main():

    # Arguments/variables
    parser = argparse.ArgumentParser()
    parser.add_argument("--file_names", help="File names", required=True)
    parser.add_argument("--sym_opts", help="Opts involving spec writing and repair", required=True)
    parser.add_argument("--dmp_opts", help="Opts involving plotting, repair, dmps", required=True)
    args = parser.parse_args()

    node = StretchSkill()
    # print(node.getJointValues())
    node.setEEFrame(EE_FRAME)
    node.setStretchFrame(STRETCH_FRAME)
    node.setOriginFrame(ORIGIN_FRAME)
    node.setDuck1Frame(DUCK1_FRAME)
    node.setDuck2Frame(DUCK2_FRAME)
    # node.teleport_base(-1.5, -0.05, 4.71)
    node.detachObject('duck_1')
    node.detachObject('duck_2')
    node.teleport_base(0.52, 0.5, 3.1415)
    node.moveArm(np.array([0.4, 0.85+0.01*np.random.random(1)[0], -10]))

    file_names = json_load_wrapper(args.file_names)
    sym_opts = json_load_wrapper(args.sym_opts)
    dmp_opts = json_load_wrapper(args.dmp_opts)

    symbols = load_symbols("/home/adam/repos/synthesis_based_repair/data/stretch/stretch_symbols.json")
    skills = load_skills_from_json("/home/adam/repos/synthesis_based_repair/data/stretch/stretch_skills.json")
    workspace_bnds = np.array(dmp_opts["workspace_bnds"])
    dmp_folder = "/home/adam/repos/synthesis_based_repair/data/dmps/"
    file_structured_slugs = "/home/adam/repos/synthesis_based_repair/data/stretch/stretch.structuredslugs"
    file_aut = "/home/adam/repos/synthesis_based_repair/data/stretch/stretch_strategy.aut"

    # Load in specification
    state_variables, action_variables = parse_spec(file_structured_slugs)
    state_def, next_states, rank_def = parse_aut(file_aut, state_variables, action_variables)

    # Find initial state
    # previous_state_number = '4'
    # previous_skill_full = 'skillStretch1to2_a_1_b_2'
    previous_state_number = '0'
    previous_skill_full = ' '
    previous_skill = previous_skill_full

    while not rospy.is_shutdown():
        world_state = node.getWorldState()
        # print(node.getJointValues())
        rospy.loginfo("Current state: {}".format(world_state))
        syms_true = find_symbols(world_state, symbols)
        rospy.loginfo("Symbols true: {}".format(syms_true))
        state_number = find_state_number(state_def, next_states, previous_state_number, previous_skill_full, syms_true)
        skill_to_run_full = find_skill_to_run(next_states, state_number)
        skill_to_run = skill_to_run_full
        rospy.loginfo("Executing skill: {}".format(skill_to_run))

        if skill_to_run != " ":
            robot_state = node.getRobotState()
            intermediate_states = node.run_skill(skill_to_run, world_state, robot_state, syms_true, skills, symbols, dmp_folder, dmp_opts)

            intermediate_states_desired = find_intermediate_symbols(intermediate_states, symbols)
            rospy.loginfo("Intermediate states visited: ")
            for i_state in intermediate_states_desired:
                rospy.loginfo(i_state)

            previous_state_number, previous_skill = update_state(intermediate_states_desired, state_number, skill_to_run_full, state_def, next_states)

            previous_skill_full = previous_skill
        else:
            previous_state_number = state_number
            previous_skill = skill_to_run
            previous_skill_full = skill_to_run_full


if __name__ == '__main__':
    # Extension, lift, yaw

    if IS_SIM:
        main()

    if not IS_SIM:
        # Retracts arm all the way, lifts, extends, and lifts a little more
        rospy.loginfo("This is running on the real stretch")
        node = StretchSkill()
        node.setStretchFrame(STRETCH_FRAME)
        node.setOriginFrame(ORIGIN_FRAME)

        pose = node.findPose('stretch')
        print(pose)

        traj = -10 * np.ones([10, 6])
        traj[:, 0] = np.linspace(0.5, 2, 10)
        traj[:, 1] = 0
        traj[:, 2] = np.pi
        print(traj)
        node.followTrajectory(traj)
