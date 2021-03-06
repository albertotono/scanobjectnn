#!/usr/bin/python3
"""Training and Validation On Classification Task."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import math
import random
import shutil
import argparse
import importlib
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(BASE_DIR, '..'))
DATA_DIR = os.path.join(ROOT_DIR, '../../../../')
import data_utils
import numpy as np
import pointfly as pf
import tensorflow as tf
from datetime import datetime
import provider
import h5py

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=1, help='GPU to use [default: GPU 0]')
parser.add_argument('--load_ckpt', '-l', help='Path to a check point file for load')

parser.add_argument('--log_dir', '-s', default='log/', help='Path to folder for saving check points and summary')
parser.add_argument('--with_bg', default = True, help='Whether to have background or not [default: True]')
parser.add_argument('--norm', default = True, help='Whether to normalize data or not [default: False]')
parser.add_argument('--center_data', default = True, help='Whether to explicitly center the data [default: False]')
parser.add_argument('--seg_weight', type=int, default=0.5, help='Segmentation weight in loss')

parser.add_argument('--train_file', default = 'h5_files/main_split/training_objectdataset_augmentedrot_scale75.h5', help='Location of training file')
parser.add_argument('--test_file', default = 'h5_files/main_split/test_objectdataset_augmentedrot_scale75.h5', help='Location of test file')

parser.add_argument('--model', '-m', default = 'pointcnn_seg', help='Model to use')
parser.add_argument('--setting', '-x', default = 'object_dataset_x3', help='Setting to use')
parser.add_argument('--epochs', help='Number of training epochs (default defined in setting)', type=int)
parser.add_argument('--batch_size', help='Batch size (default defined in setting)', type=int)
parser.add_argument('--num_point', type=int, default=1024, help='Point Number [default: 1024]')

args = parser.parse_args()

GPU_INDEX = args.gpu

time_string = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
root_folder = args.log_dir
if not os.path.exists(root_folder):
    os.makedirs(root_folder)

WITH_BG = args.with_bg
NORMALIZED = args.norm
TRAIN_FILE = args.train_file
TEST_FILE = args.test_file
CENTER_DATA = args.center_data
SEG_WEIGHT = args.seg_weight

LOG_FOUT = open(os.path.join(root_folder, 'log_train.txt'), 'w')
LOG_FOUT.write(str(args)+'\n')

def log_string(out_str):
    LOG_FOUT.write(out_str+'\n')
    LOG_FOUT.flush()
    print(out_str)


model = importlib.import_module(args.model)
setting_path = os.path.join(os.path.dirname(__file__), args.model)
sys.path.append(setting_path)
setting = importlib.import_module(args.setting)

num_epochs = args.epochs or setting.num_epochs
batch_size = args.batch_size or setting.batch_size
sample_num = args.num_point
step_val = setting.step_val
rotation_range = setting.rotation_range
rotation_range_val = setting.rotation_range_val
scaling_range = setting.scaling_range
scaling_range_val = setting.scaling_range_val
jitter = setting.jitter
jitter_val = setting.jitter_val
pool_setting_val = None if not hasattr(setting, 'pool_setting_val') else setting.pool_setting_val
pool_setting_train = None if not hasattr(setting, 'pool_setting_train') else setting.pool_setting_train

# Prepare inputs
log_string('{}-Preparing datasets...'.format(datetime.now()))

NUM_CLASSES = 15

print("Normalized: "+str(NORMALIZED))
print("Center Data: "+str(CENTER_DATA))

TRAIN_DATA, TRAIN_LABELS, TRAIN_MASKS = data_utils.load_withmask_h5(TRAIN_FILE)
TEST_DATA, TEST_LABELS, TEST_MASKS = data_utils.load_withmask_h5(TEST_FILE)      
TRAIN_MASKS = data_utils.convert_to_binary_mask(TRAIN_MASKS)
TEST_MASKS = data_utils.convert_to_binary_mask(TEST_MASKS)   

if (CENTER_DATA):
    TRAIN_DATA = data_utils.center_data(TRAIN_DATA)
    TEST_DATA = data_utils.center_data(TEST_DATA)

if (NORMALIZED):
    TRAIN_DATA = data_utils.normalize_data(TRAIN_DATA)
    TEST_DATA = data_utils.normalize_data(TEST_DATA)

num_train = len(TRAIN_DATA)
num_val = len(TEST_DATA)
print('{}-{:d}/{:d} training/validation samples.'.format(datetime.now(), num_train, num_val))

def train():
    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(GPU_INDEX)):
            # Placeholders
            xforms = tf.placeholder(tf.float32, shape=(None, 3, 3), name="xforms")
            rotations = tf.placeholder(tf.float32, shape=(None, 3, 3), name="rotations")
            jitter_range = tf.placeholder(tf.float32, shape=(1), name="jitter_range")
            global_step = tf.Variable(0, trainable=False, name='global_step')
            is_training_pl = tf.placeholder(tf.bool, name='is_training')

            pointclouds_pl = tf.placeholder(tf.float32, shape=(batch_size, sample_num, 3), name='data')
            labels_pl = tf.placeholder(tf.int32, shape=(batch_size), name='label')
            masks_pl = tf.placeholder(tf.int32, shape=(batch_size, sample_num), name='mask')

            points_augmented = pf.augment(pointclouds_pl, xforms, jitter_range)
            net = model.Net(points=points_augmented, features=None, is_training=is_training_pl, setting=setting)
            classification_logits = net.classification_logits
            segmentation_logits = net.segmentation_logits


            probs = tf.nn.softmax(classification_logits, name='probs')
            predictions = tf.argmax(probs, axis=-1, name='predictions')

            ##classification loss
            labels_2d = tf.expand_dims(labels_pl, axis=-1, name='labels_2d')
            labels_tile = tf.tile(labels_2d, (1, tf.shape(classification_logits)[1]), name='labels_tile')
            classify_loss = tf.reduce_mean(tf.losses.sparse_softmax_cross_entropy(labels=labels_tile, logits=classification_logits))

            ##segmentation loss
            per_instance_seg_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=segmentation_logits, labels=masks_pl), axis=1)
            seg_loss = tf.reduce_mean(per_instance_seg_loss)

            loss_op = (1-SEG_WEIGHT)*classify_loss + SEG_WEIGHT*seg_loss             

            tf.summary.scalar('total loss', loss_op)
            tf.summary.scalar('classify_loss', classify_loss)
            tf.summary.scalar('seg_loss', seg_loss)            

            lr_exp_op = tf.train.exponential_decay(setting.learning_rate_base, global_step, setting.decay_steps,
                                                   setting.decay_rate, staircase=True)
            lr_clip_op = tf.maximum(lr_exp_op, setting.learning_rate_min)
            _ = tf.summary.scalar('learning_rate', tensor=lr_clip_op, collections=['train'])
            reg_loss = setting.weight_decay * tf.losses.get_regularization_loss()
            if setting.optimizer == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate=lr_clip_op, epsilon=setting.epsilon)
            elif setting.optimizer == 'momentum':
                optimizer = tf.train.MomentumOptimizer(learning_rate=lr_clip_op, momentum=setting.momentum, use_nesterov=True)
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                train_op = optimizer.minimize(loss_op + reg_loss, global_step=global_step)

        # Create a session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

        saver = tf.train.Saver(max_to_keep=None)

        # backup all code
        # code_folder = os.path.abspath(os.path.dirname(__file__))
        # shutil.copytree(code_folder, os.path.join(root_folder)

        folder_ckpt = root_folder
        # if not os.path.exists(folder_ckpt):
        #     os.makedirs(folder_ckpt)

        folder_summary = os.path.join(root_folder, 'summary')
        if not os.path.exists(folder_summary):
            os.makedirs(folder_summary)          

        parameter_num = np.sum([np.prod(v.shape.as_list()) for v in tf.trainable_variables()])
        print('{}-Parameter number: {:d}.'.format(datetime.now(), parameter_num))

        sess.run(init_op)

        # saver.restore(sess, os.path.join(folder_ckpt, "model.ckpt"))
        # log_string("Model restored.")        

        merged = tf.summary.merge_all()
        train_writer = tf.summary.FileWriter(os.path.join(folder_summary, 'train'),
                                  sess.graph)
        test_writer = tf.summary.FileWriter(os.path.join(folder_summary, 'test'))

        ops = {'pointclouds_pl': pointclouds_pl,
               'labels_pl': labels_pl,
               'masks_pl': masks_pl,
               'is_training_pl': is_training_pl,
               'pred': probs,
               'seg_pred': segmentation_logits,
               'loss': loss_op,
               'classify_loss': classify_loss,
               'seg_loss': seg_loss,               
               'train_op': train_op,
               'merged': merged,
               'step': global_step,
               'xforms': xforms,
               'rotations': rotations,
               'jitter_range': jitter_range}

        for epoch in range(num_epochs):
            log_string('**** EPOCH %03d ****' % (epoch))
            sys.stdout.flush()
             
            train_one_epoch(sess, ops, train_writer)
            eval_one_epoch(sess, ops, test_writer)
            
            # Save the variables to disk.
            # if epoch % 10 == 0:
            save_path = saver.save(sess, os.path.join(folder_ckpt, "model.ckpt"))
            log_string("Model saved in file: %s" % save_path)        

def train_one_epoch(sess, ops, train_writer):
    is_training = True

    current_data, current_label, current_mask = data_utils.get_current_data_withmask_h5(TRAIN_DATA, TRAIN_LABELS, TRAIN_MASKS, sample_num)

    current_label = np.squeeze(current_label)
    current_mask = np.squeeze(current_mask)

    num_batches = current_data.shape[0]//batch_size
    total_correct = 0
    total_seen = 0
    loss_sum = 0
    total_correct_seg = 0    
    classify_loss_sum = 0
    seg_loss_sum = 0    
    for batch_idx in range(num_batches):
        start_idx = batch_idx * batch_size
        end_idx = (batch_idx+1) * batch_size

        xforms_np, rotations_np = pf.get_xforms(batch_size,
                                                rotation_range=rotation_range,
                                                scaling_range=scaling_range,
                                                order=setting.rotation_order)

        # Augment batched point clouds by rotation and jittering
        feed_dict = {ops['pointclouds_pl']: current_data[start_idx:end_idx, :, :],
                    ops['labels_pl']: current_label[start_idx:end_idx],
                    ops['masks_pl']: current_mask[start_idx:end_idx],
                    ops['is_training_pl']: is_training,
                    ops['xforms']: xforms_np,
                    ops['rotations']: rotations_np,
                    ops['jitter_range']: np.array([jitter])}

        summary, step, _, loss_val, pred_val, seg_val, classify_loss, seg_loss = sess.run([ops['merged'], ops['step'],
            ops['train_op'], ops['loss'], ops['pred'], ops['seg_pred'], ops['classify_loss'], ops['seg_loss']], feed_dict=feed_dict)

        train_writer.add_summary(summary, step)
        pred_val = np.sum(pred_val, axis=1)
        pred_val = np.argmax(pred_val, 1)
        # print(pred_val)
        # print(current_label[start_idx:end_idx])

        correct = np.sum(pred_val == current_label[start_idx:end_idx])

        seg_val = np.argmax(seg_val, 2)
        seg_correct = np.sum(seg_val == current_mask[start_idx:end_idx])
        total_correct_seg += seg_correct

        total_correct += correct
        total_seen += batch_size
        loss_sum += loss_val
        classify_loss_sum += classify_loss
        seg_loss_sum += seg_loss         

    
    log_string('mean loss: %f' % (loss_sum / float(num_batches)))
    log_string('classify mean loss: %f' % (classify_loss_sum / float(num_batches)))
    log_string('seg mean loss: %f' % (seg_loss_sum / float(num_batches)))
    log_string('accuracy: %f' % (total_correct / float(total_seen)))
    log_string('seg accuracy: %f' % (total_correct_seg / (float(total_seen)*sample_num))) 

def eval_one_epoch(sess, ops, test_writer):
    is_training = False
    total_correct = 0
    total_seen = 0
    loss_sum = 0
    classify_loss_sum = 0
    seg_loss_sum = 0
    total_correct_seg = 0

    total_seen_class = [0 for _ in range(NUM_CLASSES)]
    total_correct_class = [0 for _ in range(NUM_CLASSES)]

    current_data, current_label, current_mask = data_utils.get_current_data_withmask_h5(TEST_DATA, TEST_LABELS, TEST_MASKS, sample_num)

    current_label = np.squeeze(current_label)
    current_mask = np.squeeze(current_mask)

    num_batches = current_data.shape[0]//batch_size
        
    for batch_idx in range(num_batches):
        start_idx = batch_idx * batch_size
        end_idx = (batch_idx+1) * batch_size

        xforms_np, rotations_np = pf.get_xforms(batch_size,
                                                rotation_range=rotation_range_val,
                                                scaling_range=scaling_range_val,
                                                order=setting.rotation_order)

        # Augment batched point clouds by rotation and jittering
        feed_dict = {ops['pointclouds_pl']: current_data[start_idx:end_idx, :, :],
                    ops['labels_pl']: current_label[start_idx:end_idx],
                    ops['masks_pl']: current_mask[start_idx:end_idx],
                    ops['is_training_pl']: is_training,
                    ops['xforms']: xforms_np,
                    ops['rotations']: rotations_np,
                    ops['jitter_range']: np.array([jitter_val])}

        summary, step, loss_val, pred_val, seg_val, classify_loss, seg_loss = sess.run([ops['merged'], ops['step'],
            ops['loss'], ops['pred'], ops['seg_pred'], ops['classify_loss'], ops['seg_loss']], feed_dict=feed_dict)

        pred_val = np.sum(pred_val, axis=1)
        pred_val = np.argmax(pred_val, 1)
        correct = np.sum(pred_val == current_label[start_idx:end_idx])
        seg_val = np.argmax(seg_val, 2)
        seg_correct = np.sum(seg_val == current_mask[start_idx:end_idx])
        total_correct_seg += seg_correct         
        total_correct += correct
        total_seen += batch_size
        loss_sum += (loss_val*batch_size)
        for i in range(start_idx, end_idx):
            l = current_label[i]
            total_seen_class[l] += 1
            total_correct_class[l] += (pred_val[i-start_idx] == l)
            
    log_string('eval mean loss: %f' % (loss_sum / float(total_seen)))
    log_string('eval accuracy: %f'% (total_correct / float(total_seen)))
    log_string('eval avg class acc: %f' % (np.mean(np.array(total_correct_class)/np.array(total_seen_class,dtype=np.float))))  

    log_string('eval accuracy: %f'% (total_correct / float(total_seen)))
    log_string('eval avg class acc: %f' % (np.mean(np.array(total_correct_class)/np.array(total_seen_class,dtype=np.float))))    
    log_string('eval seg accuracy: %f' % (total_correct_seg / (float(total_seen)*sample_num))) 

if __name__ == '__main__':
    train()
