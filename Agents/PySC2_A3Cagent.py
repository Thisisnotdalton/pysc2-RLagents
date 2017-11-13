"""
PySC2_A3Cagent.py
A script for training and running an A3C agent on the PySC2 environment, with reference to DeepMind's paper:
[1] Vinyals, Oriol, et al. "Starcraft II: A new challenge for reinforcement learning." arXiv preprint arXiv:1708.04782 (2017).
Advantage estimation uses generalized advantage estimation from:
[2] Schulman, John, et al. "High-dimensional continuous control using generalized advantage estimation." arXiv preprint arXiv:1506.02438 (2015).

Credit goes to Arthur Juliani for providing for reference an implementation of A3C for the VizDoom environment
https://medium.com/emergent-future/simple-reinforcement-learning-with-tensorflow-part-8-asynchronous-actor-critic-agents-a3c-c88f72a5e9f2
https://github.com/awjuliani/DeepRL-Agents

Note:
Currently only works on the DefeatRoaches mini-game; work is in-progress for generalizing the script to run on all mini-games
"""

import threading
import multiprocessing
import numpy as np
import tensorflow as tf
import scipy.signal
from time import sleep
import os
from pysc2.env import sc2_env
from pysc2.env import environment
from pysc2.lib import actions

from SC2Definitions import *

_UNIT_TYPE = features.SCREEN_FEATURES.unit_type.index
_PLAYER_RELATIVE = features.SCREEN_FEATURES.player_relative.index
_ENEMY = 4
_SELECT_SIZE = 7

"""
Use the following command to launch Tensorboard:
tensorboard --logdir=worker_0:'./train_0',worker_1:'./train_1',worker_2:'./train_2',worker_3:'./train_3'
"""

## HELPER FUNCTIONS

# Copies one set of variables to another.
# Used to set worker network parameters to those of global network.
def update_target_graph(from_scope,to_scope):
    from_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, from_scope)
    to_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, to_scope)
    op_holder = []
    for from_var,to_var in zip(from_vars,to_vars):
        op_holder.append(to_var.assign(from_var))
    return op_holder

# Processes PySC2 observations
def process_observation(observation):
    nonspatial_size = 727
    screen_channels = 7
    multi_select_max = 100
    # is episode over?
    episode_end = observation.step_type == environment.StepType.LAST
    # reward
    reward = observation.reward
    # features
    features = observation.observation
    # nonspatial features
    # TimeStep.observation['control_groups'](10,2)
    # TimeStep.observation['single_select'](1,7)
    # TimeStep.observation['multi_select'](n,7)
    nonspatial_stack = features['control_groups'].reshape(-1)
    nonspatial_stack = np.concatenate((nonspatial_stack, features['single_select'].reshape(-1))) 
    multi_select = features['multi_select'].reshape(-1)
    # if multi_select has less than multi_select_max units, pad with zeros
    if len(multi_select) < multi_select_max * 7:
        multi_select = np.concatenate((multi_select, np.zeros(multi_select_max * 7 - len(multi_select))))
    nonspatial_stack = np.concatenate((nonspatial_stack, multi_select))
    # spatial_minimap features
    # not used for DefeatRoaches since no camera movement is required
    minimap_stack = None
    # spatial_screen features
    # TimeStep.observation['screen'][5] (player_relative)
    # TimeStep.observation['screen'][6] (unit_type)
    # TimeStep.observation['screen'][7] (selected)
    # TimeStep.observation['screen'][8] (unit_hit_points)
    # TimeStep.observation['screen'][9] (unit_hit_points_ratio)
    # TimeStep.observation['screen'][14] (unit_density)
    # TimeStep.observation['screen'][15] (unit_density_aa)
    screen_stack = np.stack((features['screen'][5], features['screen'][6], features['screen'][7], features['screen'][8], features['screen'][9], features['screen'][14], features['screen'][15]), axis=2)
    return reward, nonspatial_stack.reshape([-1,nonspatial_size]), minimap_stack, screen_stack.reshape([-1,64,64,screen_channels]), episode_end

# Discounting function used to calculate discounted returns.
def discount(x, gamma):
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

# Used to initialize weights for policy and value output layers
def normalized_columns_initializer(std=1.0):
    def _initializer(shape, dtype=None, partition_info=None):
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)
    return _initializer

# Sample from distribution of arguments
def sample_dist(dist):
    sample = np.random.choice(dist[0],p=dist[0])
    sample = np.argmax(dist == sample)
    return sample

# Structure data of AC Netowirk based on race of player
class AgentModel:
    def __ init__(self, race = 'T', isTraining = False, multi_select_max = 100, screen_size = 64, minimap_size=64, screen_channels=7):
        if race not in sc2_env.races.keys():
            raise ValueError("Invalid race selected: {0}.\n Race must be one of {1}.".format(race, sc2_env.races.keys()))
        self.screen_size = screen_size
        self.screen_channels = screen_channels
        self.minipam_size = minimap_size
        self.isTraining = isTraining
        self.multi_select_max = int(multi_select_max)
        self.race = race
        if len(ACTIONS[race]) < 1 or len(ACTIONS['N']) < 1:
            print('Classifying actions based on race...')
            classify_actions()
        self.general_actions = list(ACTIONS['N'])
        print('General Actions:\n{0}'.format(self.general_actions))
        #Limit actions based on race
        self.race_actions = list(ACTIONS[race])
        print('Race Actions:\n{0}'.format(self.race_actions))
        #Create dictionaries for quicker look-ups of action indices
        self.action_indices = {'N':{},race:{}}
        for i in range(len(self.general_actions)):
            self.action_indices['N'][self.general_actions[i]] = i
        for i in range(len(self.race_actions)):
            self.action_indices[race][self.race_actions[i]] = i
        #Create arrays counting how many times actions were used
        self.used_actions = {'N':np.zeros(len(self.general_actions)), race:np.zeros(len(self.race_actions))}
        self.action_count = len(self.general_actions)+len(self.race_actions)
        self.nonspatial_size = self.calculate_nonspatial_size()
        self.reset()
        
    def reset(self):
        #Keep track of last action used
        self.last_action_used = 0
        #Keep track of units seen for duration of game
        self.max_units_seen = np.zeros(UNIT_TYPES)
        
    def process_observation(self, obs):
        #Update units seen
        enemy_unit_types = np.zeros(self.max_units_seen.size)
        for i in range(obs.observation['screen'].shape[0]):
            for j in range(obs.observation['screen'].shape[1]):
                if obs.observation['screen'][_PLAYER_RELATIVE, i, j] == _ENEMY and obs.observation['screen'][_UNIT_TYPE, i, j] < UNIT_TYPES:
                    enemy_unit_types[obs.observation['screen'][_UNIT_TYPE, i, j]] += 1
        for i in range(1, UNIT_TYPES):
            self.max_units_seen[i] = max(self.max_units_seen[i], enemy_unit_types[i])
        screen_channels = 7
        multi_select_max = 100
        # is episode over?
        episode_end = obs.step_type == environment.StepType.LAST
        # reward
        reward = obs.reward
        # features
        features = obs.observation
        # nonspatial features
        # TimeStep.observation['control_groups'](10,2)
        # TimeStep.observation['single_select'](1,7)
        # TimeStep.observation['multi_select'](n,7)
        nonspatial_stack = features['control_groups'].reshape(-1)
        nonspatial_stack = np.concatenate((nonspatial_stack, features['single_select'].reshape(-1))) 
        multi_select = features['multi_select'].reshape(-1)
        # if multi_select has less than multi_select_max units, pad with zeros
        if len(multi_select) < multi_select_max * 7:
            multi_select = np.concatenate((multi_select, np.zeros(multi_select_max * 7 - len(multi_select))))
        nonspatial_stack = np.concatenate((nonspatial_stack, multi_select))
        # spatial_minimap features
        # not used for DefeatRoaches since no camera movement is required
        minimap_stack = None
        # spatial_screen features
        # TimeStep.observation['screen'][5] (player_relative)
        # TimeStep.observation['screen'][6] (unit_type)
        # TimeStep.observation['screen'][7] (selected)
        # TimeStep.observation['screen'][8] (unit_hit_points)
        # TimeStep.observation['screen'][9] (unit_hit_points_ratio)
        # TimeStep.observation['screen'][14] (unit_density)
        # TimeStep.observation['screen'][15] (unit_density_aa)
        screen_stack = np.stack((features['screen'][5], features['screen'][6], features['screen'][7], features['screen'][8], features['screen'][9], features['screen'][14], features['screen'][15]), axis=2)
        return reward, nonspatial_stack.reshape([-1,self.nonspatial_size]), minimap_stack, screen_stack.reshape([-1,64,64,screen_channels]), episode_end

    def get_action(self, action_index):
        if action_selected < len(self.general_actions):
            return actions.FUNCTIONS[self.general_actions[action_index]]
        else:
            action_index -= len(self.general_actions)
            return actions.FUNCTIONS[self.race_actions[action_index]]
    
    def act(self, action_selected, action_arguments):
        self.last_action_used = action_selected
        #Determine whether action was race specific or not
        if action_selected < len(self.general_actions):
            action_type = 'N'
        else:
            action_type = self.race
            action_selected -= len(self.general_actions)
        #Update how many times action was called
        self.used_actions[action_type][action_selected] += 1

        
    def calculate_nonspatial_size(self):
        #Add action space sizes for tracking which actions we can take
        size = self.action_count * 2 #Multiply by two for entries that keep track of how many times used
        #Increase size by number of unit types for enemies seen
        size += self.max_units_seen.size
        #Increase size by nonspatial structured observation data:
        #1 player_id
        #2 minerals
        #3 vespene
        #4 food used (otherwise known as supply)
        #5 food cap
        #6 food used by army
        #7 food used by workers
        #8 idle worker count
        #9 army count
        #10 warp gate count (for protoss)
        #11 larva count (for zerg)
        size += 11
        #Increase size by control groups (10,2) and single select (1,7)
        size += 20 + _SELECT_SIZE
        #Increase size by max potential for units to be selected
        size += _SELECT_SIZE * self.multi_select_max
        return size


## ACTOR-CRITIC NETWORK

class AC_Network():
    def __init__(self,scope,trainer,agentModel=None):
        self.model = agentModel           
        with tf.variable_scope(scope):
            # Architecture here follows Atari-net Agent described in [1] Section 4.3
            self.inputs_nonspatial = tf.placeholder(shape=[None,self.agent.nonspatial_size], dtype=tf.float32)
            self.inputs_spatial_screen_reshaped = tf.placeholder(shape=[None,self.model.screen_size, self.model.screen_size,self.model.screen_channels], dtype=tf.float32)
            self.nonspatial_dense = tf.layers.dense(
                inputs=self.inputs_nonspatial,
                units=32,
                activation=tf.tanh)
            self.screen_conv1 = tf.layers.conv2d(
                inputs=self.inputs_spatial_screen_reshaped,
                filters=16,
                kernel_size=[8,8],
                strides=[4,4],
                padding='valid',
                activation=tf.nn.relu)
            self.screen_conv2 = tf.layers.conv2d(
                inputs=self.screen_conv1,
                filters=32,
                kernel_size=[4,4],
                strides=[2,2],
                padding='valid',
                activation=tf.nn.relu)
            # According to [1]: "The results are concatenated and sent through a linear layer with a ReLU activation."
            self.latent_vector = tf.layers.dense(
                inputs=tf.concat([self.nonspatial_dense, tf.reshape(self.screen_conv2,shape=[-1,6*6*32])], axis=1),
                units=256,
                activation=tf.nn.relu)
            
            # Output layers for policy and value estimations
            # 12 policy networks for base actions and arguments
            #   - All modeled independently
            #   - Spatial arguments have the x and y values modeled independently as well
            # 1 value network
            self.policy_base_actions = tf.layers.dense(
                inputs=self.latent_vector,
                units=self.model.action_count,
                activation=tf.nn.softmax,
                kernel_initializer=normalized_columns_initializer(0.01))
            self.policy_arg = {}
            if scope != 'global':
                self.policy_arg_placeholder = {}
                self.policy_one_hot = {}
                self.responsible_outputs = {}
            #iterate over all function argument types
            for arg_type in actions.TYPES:
                self.policy_arg[arg_type] = []
                if scope != 'global':
                    self.policy_arg_placeholder[arg_type] = []
                    self.policy_one_hot[arg_type] = []
                for arg_size in arg_type.sizes:
                    if arg_type == actions.TYPES.screen or arg_type == actions.TYPES.screen2:
                        size = self.model.screen_size
                    elif arg_type == actions.TYPES.minimap:
                        size = self.model.minimap_size
                    else:
                        size = arg_size

                    if arg_type == actions.TYPES.queued or arg_type == actions.TYPES.select_add:
                        init_val = 1.0
                    else:
                        init_val = 0.01
                    self.policy_arg[arg_type].append(tf.layers.dense(inputs=self.latent_vector,
                                                    units=size,
                                                    activation=tf.nn.softmax,
                                                    kernel_initializer=normalized_columns_initializer(init_val)))
                    if scope != 'global':
                        self.policy_arg_placeholder[arg_type].append(tf.placeholder(shape=[None], dtype = tf.int32))
                        self.policy_one_hot.append(tf.one_hot(self.policy_arg_place_holder[arg_type][-1], size, dtype=tf.float32))

#            self.policy_arg['select_add'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=2,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(1.0))
#            self.policy_arg['queued'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=2,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(1.0))
#            self.policy_arg['select_point_act'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=4,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['select_unit_act'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=4,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['control_group_act'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=5,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['control_group_id'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=10,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['select_unit_id'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=500,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['screen_x'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=self.model.screen_size,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['screen_y'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=self.model.screen_size,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['screen2_x'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=self.model.screen_size,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
#            self.policy_arg['screen2_y'] = tf.layers.dense(
#                inputs=self.latent_vector,
#                units=self.model.screen_size,
#                activation=tf.nn.softmax,
#                kernel_initializer=normalized_columns_initializer(0.01))
            self.value = tf.layers.dense(
                inputs=self.latent_vector,
                units=1,
                kernel_initializer=normalized_columns_initializer(1.0))
            # Only the worker network need ops for loss functions and gradient updating.
            # calculates the losses
            # self.gradients - gradients of loss wrt local_vars
            # applies the gradients to update the global network
            if scope != 'global':
                #Create a dictionary to hold one_hots
                for arg_type in self.policy_arg.keys():
                    arg_one_hots = []
                    for arg_size in arg_type.sizes
                self.actions_base = tf.placeholder(shape=[None],dtype=tf.int32)
                self.actions_onehot_base = tf.one_hot(self.actions_base,self.model.action_count,dtype=tf.float32)
#                self.actions_arg_screen_x = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_screen_x = tf.one_hot(self.actions_arg_screen_x,self.model.screen_size,dtype=tf.float32)
#                self.actions_arg_screen_y = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_screen_y = tf.one_hot(self.actions_arg_screen_y,self.model.screen_size,dtype=tf.float32)
#                self.actions_arg_screen2_x = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_screen2_x = tf.one_hot(self.actions_arg_screen2_x,self.model.screen_size,dtype=tf.float32)
#                self.actions_arg_screen2_y = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_screen2_y = tf.one_hot(self.actions_arg_screen2_y,self.model.screen_size,dtype=tf.float32)
#                self.actions_arg_select_point_act = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_select_point_act = tf.one_hot(self.actions_arg_select_point_act,4,dtype=tf.float32) #float
#                self.actions_arg_select_add = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_select_add = tf.one_hot(self.actions_arg_select_add,2,dtype=tf.float32) #float
#                self.actions_arg_control_group_act = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_control_group_act = tf.one_hot(self.actions_arg_control_group_act,5,dtype=tf.float32) #float
#                self.actions_arg_control_group_id = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_control_group_id = tf.one_hot(self.actions_arg_control_group_id,10,dtype=tf.float32)
#                self.actions_arg_select_unit_id = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_select_unit_id = tf.one_hot(self.actions_arg_select_unit_id,500,dtype=tf.float32)
#                self.actions_arg_select_unit_act = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_select_unit_act = tf.one_hot(self.actions_arg_select_unit_act,4,dtype=tf.float32)
#                self.actions_arg_queued = tf.placeholder(shape=[None],dtype=tf.int32)
#                self.actions_onehot_arg_queued = tf.one_hot(self.actions_arg_queued,2,dtype=tf.float32)
                self.target_v = tf.placeholder(shape=[None],dtype=tf.float32)
                self.advantages = tf.placeholder(shape=[None],dtype=tf.float32)

                self.responsible_outputs_base = tf.reduce_sum(self.policy_base_actions * self.actions_onehot_base, [1])
                self.responsible_outputs_arg_screen_x = tf.reduce_sum(self.policy_arg_screen_x * self.actions_onehot_arg_screen_x, [1])
                self.responsible_outputs_arg_screen_y = tf.reduce_sum(self.policy_arg_screen_y * self.actions_onehot_arg_screen_y, [1])
                self.responsible_outputs_arg_screen2_x = tf.reduce_sum(self.policy_arg_screen2_x * self.actions_onehot_arg_screen2_x, [1])
                self.responsible_outputs_arg_screen2_y = tf.reduce_sum(self.policy_arg_screen2_y * self.actions_onehot_arg_screen2_y, [1])
                self.responsible_outputs_arg_select_point_act = tf.reduce_sum(self.policy_arg_select_point_act)
                self.responsible_outputs_arg_select_add = tf.reduce_sum(self.policy_arg_select_add)
                self.responsible_outputs_arg_control_group_act = tf.reduce_sum(self.policy_arg_control_group_act)
                self.responsible_outputs_arg_control_group_id = tf.reduce_sum(self.policy_arg_control_group_id)
                self.responsible_outputs_arg_select_unit_id = tf.reduce_sum(self.policy_arg_select_unit_id)
                self.responsible_outputs_arg_select_unit_act = tf.reduce_sum(self.policy_arg_select_unit_act)
                self.responsible_outputs_arg_queued = tf.reduce_sum(self.policy_arg_queued)
                
                # Loss functions
                self.value_loss = 0.5 * tf.reduce_sum(tf.square(self.target_v - tf.reshape(self.value,[-1])))

                self.log_policy_base_actions = tf.log(tf.clip_by_value(self.policy_base_actions, 1e-20, 1.0)) # avoid NaN with clipping when value in policy becomes zero
                self.entropy_base = - tf.reduce_sum(self.policy_base_actions * self.log_policy_base_actions)
                self.entropy_arg_screen_x = - tf.reduce_sum(self.policy_arg_screen_x * tf.log(tf.clip_by_value(self.policy_arg_screen_x, 1e-20, 1.0)))
                self.entropy_arg_screen_y = - tf.reduce_sum(self.policy_arg_screen_y * tf.log(tf.clip_by_value(self.policy_arg_screen_y, 1e-20, 1.0)))
                self.entropy_arg_screen2_x = - tf.reduce_sum(self.policy_arg_screen2_x * tf.log(tf.clip_by_value(self.policy_arg_screen2_x, 1e-20, 1.0)))
                self.entropy_arg_screen2_y = - tf.reduce_sum(self.policy_arg_screen2_y * tf.log(tf.clip_by_value(self.policy_arg_screen2_y, 1e-20, 1.0)))
                self.entropy_arg_select_point_act = - tf.reduce_sum(self.policy_arg_select_point_act * tf.log(tf.clip_by_value(self.policy_arg_select_point_act, 1e-20, 1.0)))
                self.entropy_arg_select_add = - tf.reduce_sum(self.policy_arg_select_add * tf.log(tf.clip_by_value(self.policy_arg_select_add, 1e-20, 1.0)))
                self.entropy_arg_control_group_act = - tf.reduce_sum(self.policy_arg_control_group_act * tf.log(tf.clip_by_value(self.policy_arg_control_group_act, 1e-20, 1.0)))
                self.entropy_arg_control_group_id = - tf.reduce_sum(self.policy_arg_control_group_id * tf.log(tf.clip_by_value(self.policy_arg_control_group_id, 1e-20, 1.0)))
                self.entropy_arg_select_unit_id = - tf.reduce_sum(self.policy_arg_select_unit_id * tf.log(tf.clip_by_value(self.policy_arg_select_unit_id, 1e-20, 1.0)))
                self.entropy_arg_select_unit_act = - tf.reduce_sum(self.policy_arg_select_unit_act * tf.log(tf.clip_by_value(self.policy_arg_select_unit_act, 1e-20, 1.0)))
                self.entropy_arg_queued = - tf.reduce_sum(self.policy_arg_queued * tf.log(tf.clip_by_value(self.policy_arg_queued, 1e-20, 1.0)))
                self.entropy = self.entropy_base + self.entropy_arg_screen_x + self.entropy_arg_screen_y + self.entropy_arg_screen2_x + self.entropy_arg_screen2_y + self.entropy_arg_select_point_act + self.entropy_arg_select_add + self.entropy_arg_control_group_act + self.entropy_arg_control_group_id + self.entropy_arg_select_unit_id + self.entropy_arg_select_unit_act + self.entropy_arg_queued

                self.policy_loss_base = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_base, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_screen_x = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_screen_x, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_screen_y = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_screen_y, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_screen2_x = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_screen2_x, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_screen2_y = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_screen2_y, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_select_point_act = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_select_point_act, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_select_add = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_select_add, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_control_group_act = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_control_group_act, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_control_group_id = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_control_group_id, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_select_unit_id = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_select_unit_id, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_select_unit_act = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_select_unit_act, 1e-20, 1.0))*self.advantages)
                self.policy_loss_arg_queued = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg_queued, 1e-20, 1.0))*self.advantages)
                self.policy_loss = self.policy_loss_base + self.policy_loss_arg_screen_x + self.policy_loss_arg_screen_y + self.policy_loss_arg_screen2_x + self.policy_loss_arg_screen2_y + self.policy_loss_arg_select_point_act + self.policy_loss_arg_select_add + self.policy_loss_arg_control_group_act + self.policy_loss_arg_control_group_id + self.policy_loss_arg_select_unit_id + self.policy_loss_arg_select_unit_act + self.policy_loss_arg_queued

                self.loss = 0.5 * self.value_loss + self.policy_loss - self.entropy * 0.01

                # Get gradients from local network using local losses
                local_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
                self.gradients = tf.gradients(self.loss,local_vars)
                self.var_norms = tf.global_norm(local_vars)
                grads,self.grad_norms = tf.clip_by_global_norm(self.gradients,40.0)
                
                # Apply local gradients to global network
                global_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'global')
                self.apply_grads = trainer.apply_gradients(zip(grads,global_vars))

## WORKER AGENT

class Worker():
    def __init__(self,name,trainer,model_path,global_episodes):
        self.name = "worker_" + str(name)
        self.number = name        
        self.model_path = model_path
        self.trainer = trainer
        self.global_episodes = global_episodes
        self.increment = self.global_episodes.assign_add(1)
        self.episode_rewards = []
        self.episode_lengths = []
        self.episode_mean_values = []
        self.summary_writer = tf.summary.FileWriter("train_"+str(self.number))

        #Create the local copy of the network and the tensorflow op to copy global paramters to local network
        self.local_AC = AC_Network(self.name,trainer)
        self.update_local_ops = update_target_graph('global',self.name)        
        
        self.env = sc2_env.SC2Env(map_name="DefeatRoaches")

        
    def train(self,rollout,sess,gamma,bootstrap_value):
        rollout = np.array(rollout)
        obs_screen = rollout[:,0]
        obs_nonspatial = rollout[:,1]
        actions_base = rollout[:,2]
        actions_arg_screen_x = rollout[:,3]
        actions_arg_screen_y = rollout[:,4]
        actions_arg_screen2_x = rollout[:,5]
        actions_arg_screen2_y = rollout[:,6]
        actions_arg_select_point_act = rollout[:,7]
        actions_arg_select_add = rollout[:,8]
        actions_arg_control_group_act = rollout[:,9]
        actions_arg_control_group_id = rollout[:,10]
        actions_arg_select_unit_id = rollout[:,11]
        actions_arg_select_unit_act = rollout[:,12]
        actions_arg_queued = rollout[:,13]
        rewards = rollout[:,14]
        next_obs_screen = rollout[:,15]
        next_obs_nonspatial = rollout[:,16]
        values = rollout[:,18]
        
        # Here we take the rewards and values from the rollout, and use them to calculate the advantage and discounted returns. 
        # The advantage function uses generalized advantage estimation from [2]
        self.rewards_plus = np.asarray(rewards.tolist() + [bootstrap_value])
        discounted_rewards = discount(self.rewards_plus,gamma)[:-1]
        self.value_plus = np.asarray(values.tolist() + [bootstrap_value])
        advantages = rewards + gamma * self.value_plus[1:] - self.value_plus[:-1]
        advantages = discount(advantages,gamma)

        # Update the global network using gradients from loss
        # Generate network statistics to periodically save
        feed_dict = {self.local_AC.target_v:discounted_rewards,
            self.local_AC.inputs_spatial_screen_reshaped:np.stack(obs_screen).reshape(-1,self.mode.screen_size,self.model.screen_size,self.model.screen_channels),
            self.local_AC.inputs_nonspatial:np.stack(obs_nonspatial).reshape(-1,self.model.nonspatial_size),
            self.local_AC.actions_base:actions_base,
            self.local_AC.actions_arg_screen_x:actions_arg_screen_x,
            self.local_AC.actions_arg_screen_y:actions_arg_screen_y,
            self.local_AC.actions_arg_screen2_x:actions_arg_screen2_x,
            self.local_AC.actions_arg_screen2_y:actions_arg_screen2_y,
            self.local_AC.actions_arg_select_point_act:actions_arg_select_point_act,
            self.local_AC.actions_arg_select_add:actions_arg_select_add,
            self.local_AC.actions_arg_control_group_act:actions_arg_control_group_act,
            self.local_AC.actions_arg_control_group_id:actions_arg_control_group_id,
            self.local_AC.actions_arg_select_unit_id:actions_arg_select_unit_id,
            self.local_AC.actions_arg_select_unit_act:actions_arg_select_unit_act,
            self.local_AC.actions_arg_queued:actions_arg_queued,
            self.local_AC.advantages:advantages}
        v_l,p_l,e_l,g_n,v_n, _ = sess.run([self.local_AC.value_loss,
            self.local_AC.policy_loss,
            self.local_AC.entropy,
            self.local_AC.grad_norms,
            self.local_AC.var_norms,
            self.local_AC.apply_grads],
            feed_dict=feed_dict)
        return v_l / len(rollout),p_l / len(rollout),e_l / len(rollout), g_n,v_n
        
    def work(self,max_episode_length,gamma,sess,coord,saver):
        episode_count = sess.run(self.global_episodes)
        total_steps = 0
        print ("Starting worker " + str(self.number))
        with sess.as_default(), sess.graph.as_default():                 
            while not coord.should_stop():
                # Download copy of parameters from global network
                sess.run(self.update_local_ops)

                episode_buffer = []
                episode_values = []
                episode_frames = []
                episode_reward = 0
                episode_step_count = 0
                d = False
                
                # Start new episode
                obs = self.env.reset()
                self.model.reset()
                episode_frames.append(obs[0])
                reward, nonspatial_stack, minimap_stack, screen_stack, episode_end = self.model.process_observation(obs[0])
                s_screen = screen_stack
                s_nonspatial = nonspatial_stack
                
                while not episode_end:
                    # Take an action using distributions from policy networks' outputs.
                    base_action_dist, screen_x_dist, screen_y_dist, screen2_x_dist, screen2_y_dist, select_point_act_dist,select_add_dist,control_group_act_dist,control_group_id_dist,select_unit_id_dist,select_unit_act_dist,queued_dist,v = sess.run([
                        self.local_AC.policy_base_actions, 
                        self.local_AC.policy_arg_screen_x, 
                        self.local_AC.policy_arg_screen_y, 
                        self.local_AC.policy_arg_screen2_x, 
                        self.local_AC.policy_arg_screen2_y, 
                        self.local_AC.policy_arg_select_point_act,
                        self.local_AC.policy_arg_select_add,
                        self.local_AC.policy_arg_control_group_act,
                        self.local_AC.policy_arg_control_group_id,
                        self.local_AC.policy_arg_select_unit_id,
                        self.local_AC.policy_arg_select_unit_act,
                        self.local_AC.policy_arg_queued,
                        self.local_AC.value],
                        feed_dict={self.local_AC.inputs_spatial_screen_reshaped: screen_stack,
                        self.local_AC.inputs_nonspatial: nonspatial_stack})

                    # Apply filter to remove unavailable actions and then renormalize
                    index2action_id = {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:7, 7:12, 8:13, 9:274, 10:331, 11:332, 12:333, 13:334, 14:451, 15:452, 16:453}
                    for index, action in enumerate(base_action_dist[0]):
                        action_id = index2action_id[index]
                        if action_id not in obs[0].observation['available_actions']:
                            base_action_dist[0][index] = 0
                    if np.sum(base_action_dist[0]) != 1:
                        current_sum = np.sum(base_action_dist[0])
                        base_action_dist[0] /= current_sum
                        
                    base_action = sample_dist(base_action_dist)
                    arg_screen_x = sample_dist(screen_x_dist)
                    arg_screen_y = sample_dist(screen_y_dist)
                    arg_screen2_x = sample_dist(screen2_x_dist)
                    arg_screen2_y = sample_dist(screen2_y_dist)
                    arg_select_point_act = sample_dist(select_point_act_dist)
                    arg_select_add = sample_dist(select_add_dist)
                    arg_control_group_act = sample_dist(control_group_act_dist)
                    arg_control_group_id = sample_dist(control_group_id_dist)
                    arg_select_unit_id = sample_dist(select_unit_id_dist)
                    arg_select_unit_act = sample_dist(select_unit_act_dist)
                    arg_queued = sample_dist(queued_dist)
                    #Convert selected action to function class
                    fun = self.model.get_action(base_action)
                    #Select arguments based on function
                    args = []
                    for argType in fun.args:
                        #TODO
                        argsOfType=[]
                        for i in range(argType.size):
                            
                    # 17 relevant base actions
                    if base_action == 0:
                        # 0/no_op
                        action_id = 0
                        arguments = []
                    elif base_action == 1:
                        # 1/move_camera
                        action_id = 1
                        arguments = [[arg_screen_x, arg_screen_y]]
                    elif base_action == 2:
                        # 2/select_point
                        action_id = 2
                        arguments = [[arg_select_point_act],[arg_screen_x, arg_screen_y]]
                    elif base_action == 3:
                        # 3/select_rect
                        action_id = 3
                        arguments = [[arg_select_add],[arg_screen_x, arg_screen_y],[arg_screen2_x, arg_screen2_y]]
                    elif base_action == 4:
                        # 4/select_control_group
                        action_id = 4
                        arguments = [[arg_control_group_act],[arg_control_group_id]]
                    elif base_action == 5:
                        # 5/select_unit 
                        action_id = 5
                        arguments = [[arg_select_unit_act],[arg_select_unit_id]]
                    elif base_action == 6:
                        # 7/select_army
                        action_id = 7
                        arguments = [[arg_select_add]]
                    elif base_action == 7:
                        # 12/Attack_screen
                        action_id = 12
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 8:
                        # 13/Attack_minimap
                        action_id = 13
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 9:
                        # 274/HoldPosition_quick
                        action_id = 274
                        arguments = [[arg_queued]]
                    elif base_action == 10:
                        # 331/Move_screen
                        action_id = 331
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 11:
                        # 332/Move_minimap
                        action_id = 332
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 12:
                        # 333/Patrol_screen
                        action_id = 333
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 13:
                        # 334/Patrol_minimap
                        action_id = 334
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 14:
                        # 451/Smart_screen 
                        action_id = 451
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 15:
                        # 452/Smart_minimap
                        action_id = 452
                        arguments = [[arg_queued],[arg_screen_x, arg_screen_y]]
                    elif base_action == 16:
                        # 453/Stop_quick
                        action_id = 453
                        arguments = [[arg_queued]]
                    
                    a = actions.FunctionCall(action_id, arguments)
                    obs = self.env.step(actions=[a])
                    r, nonspatial_stack, minimap_stack, screen_stack, episode_end = process_observation(obs[0])

                    if not episode_end:
                        episode_frames.append(obs[0])
                        s1_screen = screen_stack
                        s1_nonspatial = nonspatial_stack
                    else:
                        s1_screen = s_screen
                        s1_nonspatial = s_nonspatial
                    
                    # Append latest state to buffer
                    episode_buffer.append([s_screen, s_nonspatial,base_action,arg_screen_x,arg_screen_y,arg_screen2_x,arg_screen2_y,arg_select_point_act,arg_select_add,arg_control_group_act,arg_control_group_id,arg_select_unit_id,arg_select_unit_act,arg_queued,r,s1_screen, s1_nonspatial,d,v[0,0]])
                    episode_values.append(v[0,0])

                    episode_reward += r
                    s_screen = s1_screen
                    s_nonspatial = s1_nonspatial                 
                    total_steps += 1
                    episode_step_count += 1
                    
                    global _steps
                    _steps += 1
                    
                    # If the episode hasn't ended, but the experience buffer is full, then we make an update step using that experience rollout.
                    if len(episode_buffer) == 30 and not episode_end and episode_step_count != max_episode_length - 1:
                        # Since we don't know what the true final return is, we "bootstrap" from our current value estimation.
                        v1 = sess.run(self.local_AC.value, 
                            feed_dict={self.local_AC.inputs_spatial_screen_reshaped: screen_stack,self.local_AC.inputs_nonspatial: nonspatial_stack})[0,0]
                        v_l,p_l,e_l,g_n,v_n = self.train(episode_buffer,sess,gamma,v1)
                        episode_buffer = []
                        sess.run(self.update_local_ops)
                    if episode_end:
                        break
                                            
                self.episode_rewards.append(episode_reward)
                self.episode_lengths.append(episode_step_count)
                self.episode_mean_values.append(np.mean(episode_values))
                episode_count += 1

                global _max_score, _running_avg_score, _episodes
                if _max_score < episode_reward:
                	_max_score = episode_reward
                _running_avg_score = (2.0 / 101) * (episode_reward - _running_avg_score) + _running_avg_score
                _episodes += 1

                print("{} Step #{} Episode #{} Reward: {}".format(self.name, total_steps, episode_count, episode_reward))
                print("Total Steps: {}\tTotal Episodes: {}\tMax Score: {}\tAvg Score: {}".format(_steps, _episodes, _max_score, _running_avg_score))

                # Update the network using the episode buffer at the end of the episode.
                if len(episode_buffer) != 0:
                    v_l,p_l,e_l,g_n,v_n = self.train(episode_buffer,sess,gamma,0.0)

                if episode_count % 5 == 0 and episode_count != 0:
                    if self.name == 'worker_0' and episode_count % 25 == 0:
                        time_per_step = 0.05
                        images = np.array(episode_frames)
                    if episode_count % 250 == 0 and self.name == 'worker_0':
                        saver.save(sess,self.model_path+'/model-'+str(episode_count)+'.cptk')
                        print ("Saved Model")

                    mean_reward = np.mean(self.episode_rewards[-5:])
                    mean_length = np.mean(self.episode_lengths[-5:])
                    mean_value = np.mean(self.episode_mean_values[-5:])
                    summary = tf.Summary()
                    summary.value.add(tag='Perf/Reward', simple_value=float(mean_reward))
                    summary.value.add(tag='Perf/Length', simple_value=float(mean_length))
                    summary.value.add(tag='Perf/Value', simple_value=float(mean_value))
                    summary.value.add(tag='Losses/Value Loss', simple_value=float(v_l))
                    summary.value.add(tag='Losses/Policy Loss', simple_value=float(p_l))
                    summary.value.add(tag='Losses/Entropy', simple_value=float(e_l))
                    summary.value.add(tag='Losses/Grad Norm', simple_value=float(g_n))
                    summary.value.add(tag='Losses/Var Norm', simple_value=float(v_n))
                    self.summary_writer.add_summary(summary, episode_count)

                    self.summary_writer.flush()
                if self.name == 'worker_0':
                    sess.run(self.increment)

def main():
    max_episode_length = 300
    gamma = .99 # discount rate for advantage estimation and reward discounting
    load_model = False
    model_path = './model'

    global _max_score, _running_avg_score, _steps, _episodes
    _max_score = -9
    _running_avg_score = -9
    _steps = 0
    _episodes = 0

    tf.reset_default_graph()

    if not os.path.exists(model_path):
        os.makedirs(model_path)

    with tf.device("/cpu:0"): 
        global_episodes = tf.Variable(0,dtype=tf.int32,name='global_episodes',trainable=False)
        trainer = tf.train.AdamOptimizer(learning_rate=1e-4)
        master_network = AC_Network('global',None) # Generate global network
        num_workers = multiprocessing.cpu_count() # Set workers to number of available CPU threads
        workers = []
        # Create worker classes
        for i in range(num_workers):
            workers.append(Worker(i,trainer,model_path,global_episodes))
        saver = tf.train.Saver(max_to_keep=5)

    with tf.Session() as sess:
        coord = tf.train.Coordinator()
        if load_model == True:
            print ('Loading Model...')
            ckpt = tf.train.get_checkpoint_state(model_path)
            saver.restore(sess,ckpt.model_checkpoint_path)
        else:
            sess.run(tf.global_variables_initializer())
            
        # This is where the asynchronous magic happens.
        # Start the "work" process for each worker in a separate thread.
        worker_threads = []
        for worker in workers:
            worker_work = lambda: worker.work(max_episode_length,gamma,sess,coord,saver)
            t = threading.Thread(target=(worker_work))
            t.start()
            sleep(0.5)
            worker_threads.append(t)
        coord.join(worker_threads)

if __name__ == '__main__':
    import sys
    from absl import flags
    FLAGS = flags.FLAGS
    FLAGS(sys.argv)
    main()
