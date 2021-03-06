#!/usr/bin/env python
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os, sys, shutil, time
import random
import threading
try:
    import queue as Queue
except ImportError:
    import Queue
import numpy as np
import time
import logging

from io_func import sparse_tuple_from
from io_func.kaldi_io_parallel import KaldiDataReadParallel
from feat_process.feature_transform import FeatureTransform
from parse_args import parse_args
from model.lstm_model import ProjConfig, LSTM_Model
from util.tensor_io import print_trainable_variables

import tensorflow as tf



class train_class(object):
    def __init__(self, conf_dict):
        self.nnet_conf = ProjConfig()
        self.nnet_conf.initial(conf_dict)

        self.kaldi_io_nstream = None
        feat_trans = FeatureTransform()
        feat_trans.LoadTransform(conf_dict['feature_transfile'])
        # init train file
        self.kaldi_io_nstream_train = KaldiDataReadParallel()
        self.input_dim = self.kaldi_io_nstream_train.Initialize(conf_dict, 
                scp_file = conf_dict['scp_file'], label = conf_dict['label'], 
                feature_transform = feat_trans, criterion = 'ce' )

        # init cv file
        self.kaldi_io_nstream_cv = KaldiDataReadParallel()
        self.kaldi_io_nstream_cv.Initialize(conf_dict, 
                scp_file = conf_dict['cv_scp'], label = conf_dict['cv_label'],
                feature_transform = feat_trans, criterion = 'ce')

        self.num_batch_total = 0
        self.num_frames_total = 0

        logging.info(self.nnet_conf.__repr__())
        logging.info(self.kaldi_io_nstream_train.__repr__())
        logging.info(self.kaldi_io_nstream_cv.__repr__())
        
        self.print_trainable_variables = False
        if conf_dict.has_key('print_trainable_variables'):
            self.print_trainable_variables = conf_dict['print_trainable_variables']
        self.tf_async_model_prefix = conf_dict['checkpoint_dir']
        self.num_threads = conf_dict['num_threads']
        self.queue_cache = conf_dict['queue_cache']
        self.input_queue = Queue.Queue(self.queue_cache)
        self.acc_label_error_rate = []
        for i in range(self.num_threads):
            self.acc_label_error_rate.append(1.1)
        if conf_dict.has_key('use_normal'):
            self.use_normal = conf_dict['use_normal']
        else:
            self.use_normal = False
        if conf_dict.has_key('use_sgd'):
            self.use_sgd = conf_dict['use_sgd']
        else:
            self.use_sgd = True

        if conf_dict.has_key('restore_training'):
            self.restore_training = conf_dict['restore_training']
        else:
            self.restore_training = False

    def get_num(self,str):
        return int(str.split('/')[-1].split('_')[0])
                    #model_48434.ckpt.final
    def construct_graph(self):
        with tf.Graph().as_default():
            self.run_ops = []
            #self.X = tf.placeholder(tf.float32, [None, None, self.input_dim], name='feature')
            print(self.nnet_conf.num_frames_batch,self.nnet_conf.batch_size,self.input_dim)
            self.X = tf.placeholder(tf.float32, [self.nnet_conf.num_frames_batch, self.nnet_conf.batch_size, self.input_dim], name='feature')
            #self.Y = tf.sparse_placeholder(tf.int32, name="labels")
            self.Y = tf.placeholder(tf.int32, [self.nnet_conf.batch_size, self.nnet_conf.num_frames_batch], name="labels")
            self.seq_len = tf.placeholder(tf.int32,[None], name = 'seq_len')

            self.learning_rate_var = tf.Variable(float(self.nnet_conf.learning_rate), trainable=False, name='learning_rate')
            if self.use_sgd:
                optimizer = tf.train.GradientDescentOptimizer(self.learning_rate_var)
            else:
                optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate_var, beta1=0.9, beta2=0.999, epsilon=1e-08)

            for i in range(self.num_threads):
                with tf.device("/gpu:%d" % i):
                    initializer = tf.random_uniform_initializer(
                            -self.nnet_conf.init_scale, self.nnet_conf.init_scale)
                    model = LSTM_Model(self.nnet_conf)
                    mean_loss, ce_loss , rnn_keep_state_op, rnn_state_zero_op ,label_error_rate, softval = model.ce_train(self.X, self.Y, self.seq_len)
                    if self.use_sgd and self.use_normal:
                        tvars = tf.trainable_variables()
                        grads, _ = tf.clip_by_global_norm(tf.gradients(
                            mean_loss, tvars), self.nnet_conf.grad_clip)
                        train_op = optimizer.apply_gradients(
                                zip(grads, tvars),
                                global_step=tf.contrib.framework.get_or_create_global_step())
                    else:
                        train_op = optimizer.minimize(mean_loss)

                    run_op = {'train_op':train_op,
                            'mean_loss':mean_loss,
                            'ce_loss':ce_loss,
                            'rnn_keep_state_op':rnn_keep_state_op,
                            'rnn_state_zero_op':rnn_state_zero_op,
                            'label_error_rate':label_error_rate,
                            'softval':softval}
                    self.run_ops.append(run_op)
                    tf.get_variable_scope().reuse_variables()

            gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.95)
            self.sess = tf.Session(config=tf.ConfigProto(
                intra_op_parallelism_threads=self.num_threads, allow_soft_placement=True,
                log_device_placement=False, gpu_options=gpu_options))
            init = tf.group(tf.global_variables_initializer(),tf.local_variables_initializer())
            tmp_variables=tf.trainable_variables()
            self.saver = tf.train.Saver(tmp_variables, max_to_keep=100)
            #self.saver = tf.train.Saver(max_to_keep=100)
            if self.restore_training:
                self.sess.run(init)
                ckpt = tf.train.get_checkpoint_state(self.tf_async_model_prefix)
                if ckpt and ckpt.model_checkpoint_path:
                    logging.info("restore training")
                    self.saver.restore(self.sess, ckpt.model_checkpoint_path)
                    self.num_batch_total = self.get_num(ckpt.model_checkpoint_path)
                    if self.print_trainable_variables == True:
                        print_trainable_variables(self.sess, ckpt.model_checkpoint_path+'.txt')
                        sys.exit(0)

                    logging.info('model:'+ckpt.model_checkpoint_path)
                    logging.info('restore learn_rate:'+str(self.sess.run(self.learning_rate_var)))
                    #print('*******************',self.num_batch_total)
                    #time.sleep(3)
                    #model_48434.ckpt.final
                    #print("ckpt.model_checkpoint_path",ckpt.model_checkpoint_path)
                    #print("self.tf_async_model_prefix",self.tf_async_model_prefix)
                    #self.saver.restore(self.sess, self.tf_async_model_prefix)
                    
                else:
                    logging.info('No checkpoint file found')
                    self.sess.run(init)
                    logging.info('init learn_rate:'+str(self.sess.run(self.learning_rate_var)))
            else:
                self.sess.run(init)

            self.total_variables = np.sum([np.prod(v.get_shape().as_list()) for v in tf.trainable_variables()])
            logging.info('total parameters : %d' % self.total_variables)

    def train_function(self, gpu_id, run_op, thread_name):
        total_acc_error_rate = 0.0
        num_batch = 0
        num_bptt = 0
        while True:
            time1=time.time()
            feat,label,length = self.get_feat_and_label()
            if feat is None:
                logging.info('train thread ok: %s' % thread_name)
                break
            time2=time.time()
            print('******time:',time2-time1, thread_name)
            self.sess.run(run_op['rnn_state_zero_op'])
            for i in range(len(feat)):
                feed_dict = {self.X : feat[i], self.Y : label[i], self.seq_len : length[i]}
                run_need_op = {'train_op':run_op['train_op'],
                        'mean_loss':run_op['mean_loss'],
                        'ce_loss':run_op['ce_loss'],
                        'rnn_keep_state_op':run_op['rnn_keep_state_op'],
                        'label_error_rate':run_op['label_error_rate']}
                time3 = time.time()
                calculate_return = self.sess.run(run_need_op, feed_dict = feed_dict)
                print('mean_loss:',calculate_return['mean_loss'])
                #print('ce_loss:',calculate_return['ce_loss'])
                #self.sess.run(run_op['rnn_keep_state_op'])
                time4 = time.time()
                print(num_batch," time:",time4-time3)
                print('label_error_rate:',calculate_return['label_error_rate'])
                total_acc_error_rate += calculate_return['label_error_rate']
                num_bptt += 1
            time5=time.time()

            print(num_batch," time:",time2-time1,time5-time2)

            num_batch += 1
            #total_acc_error_rate += calculate_return['label_error_rate']
            self.acc_label_error_rate[gpu_id] = total_acc_error_rate / num_bptt
        self.acc_label_error_rate[gpu_id] = total_acc_error_rate / num_bptt

    def cv_function(self, gpu_id, run_op, thread_name):
        total_acc_error_rate = 0.0
        num_batch = 0
        num_bptt = 0
        while True:
            feat,label,length = self.get_feat_and_label()
            if feat is None:
                logging.info('cv ok : %s\n' % thread_name)
                break
            self.sess.run(run_op['rnn_state_zero_op'])
            for i in range(len(feat)):
                feed_dict = {self.X : feat[i], self.Y : label[i], self.seq_len : length[i]}
                run_need_op = {'mean_loss':run_op['mean_loss'],
                        'ce_loss':run_op['ce_loss'],
                        'rnn_keep_state_op':run_op['rnn_keep_state_op'],
                        'label_error_rate':run_op['label_error_rate']}
                        #'softval':run_op['softval']}
                calculate_return = self.sess.run(run_need_op, feed_dict = feed_dict)
                total_acc_error_rate += calculate_return['label_error_rate']
#                print('feat:',feat[i])
                print('label_error_rate:',calculate_return['label_error_rate'])
                print('mean_loss:',calculate_return['mean_loss'])
                print('ce_loss', calculate_return['ce_loss'])
#                print(i,'softval', calculate_return['softval'])
#                print('rnn_keep_state_op', calculate_return['rnn_keep_state_op'])
                num_bptt += 1
            num_batch += 1
            self.acc_label_error_rate[gpu_id] = total_acc_error_rate / num_bptt
        self.acc_label_error_rate[gpu_id] = total_acc_error_rate / num_bptt

    def get_feat_and_label(self):
        return self.input_queue.get()

    def input_feat_and_label(self):
        feat,label,length = self.kaldi_io_nstream.LoadNextNstreams()
        if length is None:
            return False
        if len(label) != self.nnet_conf.batch_size:
             return False
        sparse_label = sparse_tuple_from(label)
        self.input_queue.put((feat,sparse_label,length))
        self.num_batch_total += 1
        for i in length:
            self.num_frames_total += i
        print('total_batch_num**********',self.num_batch_total,'***********')
        return True
    
    def input_ce_feat_and_label(self):
        feat_array, label_array, length_array = self.kaldi_io_nstream.SliceLoadNextNstreams()
        if length_array is None:
            return False
        if len(label_array[0]) != self.nnet_conf.batch_size:
            return False
        #process feature
        #sparse_label_array = []
        #for lab in label:
        #    sparse_label_array.append(sparse_tuple_from(lab))
        self.input_queue.put((feat_array, label_array, length_array))
        self.num_batch_total += 1
        for batch_len in length_array:
            for i in batch_len:
                self.num_frames_total += i
        print('total_batch_num**********',self.num_batch_total,'***********')
        return True

    def cv_logic(self):
        self.kaldi_io_nstream = self.kaldi_io_nstream_cv
        train_thread = []
        #first start cv thread
        for i in range(self.num_threads):
            train_thread.append(threading.Thread(group=None, target=self.cv_function,
                args=(i, self.run_ops[i], 'thread_hubo_'+str(i)), name='thread_hubo_'+str(i)))

        for thr in train_thread:
            thr.start()

        logging.info('start cv thread.')
        while True:
            # input data
            if self.input_ce_feat_and_label():
                continue
            break
        logging.info('cv read feat ok')

        for thr in train_thread:
            self.input_queue.put((None, None, None))

        while True:
            if self.input_queue.empty():
                logging.info('cv is ok')
                break;

        for thr in train_thread:
            thr.join()
            logging.info('join cv thread %s' % thr.name)
        
        tmp_label_error_rate = self.get_avergae_label_error_rate()
        self.kaldi_io_nstream.Reset()
        self.reset_acc()
        return tmp_label_error_rate

    def train_logic(self):
        self.kaldi_io_nstream = self.kaldi_io_nstream_train 
        train_thread = []
        #first start train thread
        for i in range(self.num_threads):
            #self.acc_label_error_rate.append(1.0)
            train_thread.append(threading.Thread(group=None, target=self.train_function,
                args=(i, self.run_ops[i], 'thread_hubo_'+str(i)), name='thread_hubo_'+str(i)))

        for thr in train_thread:
            thr.start()

        logging.info('start train thread ok.\n')

        all_lab_err_rate = []
        for i in range(5):
            all_lab_err_rate.append(1.1)

        while True:
            # save model
            if self.num_batch_total % 1000 == 0:
                while True:
                    #print('wait save mode')
                    time.sleep(0.5)
                    if self.input_queue.empty():
                        checkpoint_path = os.path.join(self.tf_async_model_prefix, str(self.num_batch_total)+'_model'+'.ckpt')
                        logging.info('save model: '+checkpoint_path+ 
                                ' --- learn_rate: ' + 
                                str(self.sess.run(self.learning_rate_var)))
                        self.saver.save(self.sess, checkpoint_path)

                        if self.num_batch_total == 0:
                            break

                        curr_lab_err_rate = self.get_avergae_label_error_rate()
                        all_lab_err_rate.sort()
                        for i in range(len(all_lab_err_rate)):
                            if curr_lab_err_rate < all_lab_err_rate[i]:
                                all_lab_err_rate[len(all_lab_err_rate)-1] = curr_lab_err_rate
                                break
                            if i == len(all_lab_err_rate)-1:
                                train_logic.decay_learning_rate(0.5)
                                all_lab_err_rate[len(all_lab_err_rate)-1] = curr_lab_err_rate
                        break
            # input data
            if self.input_ce_feat_and_label():
                continue
            break
        time.sleep(1)
        logging.info('read feat ok')

        '''
            end train
        '''
        for thr in train_thread:
            self.input_queue.put((None, None, None))

        while True:
            if self.input_queue.empty():
                logging.info('train is end')
                checkpoint_path = os.path.join(self.tf_async_model_prefix, str(self.num_batch_total)+'_model'+'.ckpt')
                self.saver.save(self.sess, checkpoint_path+'.final')
                break;
        '''
            train is end
        '''
        for thr in train_thread:
            thr.join()
            logging.info('join thread %s' % thr.name)

        tmp_label_error_rate = self.get_avergae_label_error_rate()
        self.kaldi_io_nstream.Reset()
        self.reset_acc()
        return tmp_label_error_rate

    def decay_learning_rate(self, lr_decay_factor):
        learning_rate_decay_op = self.learning_rate_var.assign(tf.multiply(self.learning_rate_var, lr_decay_factor))
        self.sess.run(learning_rate_decay_op)
        logging.info('learn_rate decay to '+str(self.sess.run(self.learning_rate_var)))
        logging.info('lr_decay_factor is '+str(lr_decay_factor))
#        return learning_rate_decay_op

    def get_avergae_label_error_rate(self):
        average_label_error_rate = 0.0 
        for i in range(self.num_threads):
            average_label_error_rate += self.acc_label_error_rate[i]
        average_label_error_rate /= self.num_threads
        logging.info("average label error rate : %f" % average_label_error_rate)
        return average_label_error_rate

    def reset_acc(self):
        for i in range(len(self.acc_label_error_rate)):
            self.acc_label_error_rate[i] = 1.1


if __name__ == "__main__":
    #first read parameters
    # read config file
    conf_dict = parse_args(sys.argv[1:])
    
    # Create checkpoint dir if needed
    if not os.path.exists(conf_dict["checkpoint_dir"]):
        os.makedirs(conf_dict["checkpoint_dir"])

    # Set logging framework
    if conf_dict["log_file"] is not None:
        logging.basicConfig(filename = conf_dict["log_file"])
        logging.getLogger().setLevel(conf_dict["log_level"])
    else:
        raise 'no log file in config file'

    logging.info(conf_dict)

    train_logic = train_class(conf_dict)
    train_logic.construct_graph()
    iter = 0
    err_rate = 0.429939
    while iter < 3:
        tmp_err_rate = train_logic.train_logic()
        tmp_cv_err_rate = train_logic.cv_logic()
        logging.info("iter %d: train average label error rate : %f\n" % (iter,tmp_err_rate))
        logging.info("iter %d: cv average label error rate : %f\n" % (iter,tmp_cv_err_rate))
        iter += 1
        if tmp_cv_err_rate > 1.0:
            if err_rate != 1.0:
                print('this is a error!')
            continue
        if err_rate > (tmp_cv_err_rate + 0.005):
            err_rate = tmp_cv_err_rate
        else:
            train_logic.decay_learning_rate(0.5)
        #time.sleep(5)
    logging.info('end\n')


