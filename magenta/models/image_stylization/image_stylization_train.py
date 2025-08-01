# Copyright 2025 The Magenta Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Trains the N-styles style transfer model."""
import ast
import os

from magenta.models.image_stylization import image_utils
from magenta.models.image_stylization import learning
from magenta.models.image_stylization import model
from magenta.models.image_stylization import vgg
import tensorflow.compat.v1 as tf
import tf_slim as slim

DEFAULT_CONTENT_WEIGHTS = '{"vgg_16/conv3": 1.0}'
DEFAULT_STYLE_WEIGHTS = ('{"vgg_16/conv1": 1e-4, "vgg_16/conv2": 1e-4,'
                         ' "vgg_16/conv3": 1e-4, "vgg_16/conv4": 1e-4}')

flags = tf.app.flags
flags.DEFINE_float('clip_gradient_norm', 0, 'Clip gradients to this norm')
flags.DEFINE_float('learning_rate', 1e-3, 'Learning rate')
flags.DEFINE_integer('batch_size', 16, 'Batch size.')
flags.DEFINE_integer('image_size', 256, 'Image size.')
flags.DEFINE_integer('ps_tasks', 0,
                     'Number of parameter servers. If 0, parameters '
                     'are handled locally by the worker.')
flags.DEFINE_integer('num_styles', None, 'Number of styles.')
flags.DEFINE_float('alpha', 1.0, 'Width multiplier')
flags.DEFINE_integer('save_summaries_secs', 15,
                     'Frequency at which summaries are saved, in seconds.')
flags.DEFINE_integer('save_interval_secs', 15,
                     'Frequency at which the model is saved, in seconds.')
flags.DEFINE_integer('task', 0,
                     'Task ID. Used when training with multiple '
                     'workers to identify each worker.')
flags.DEFINE_integer('train_steps', 40000, 'Number of training steps.')
flags.DEFINE_string('content_weights', DEFAULT_CONTENT_WEIGHTS,
                    'Content weights')
flags.DEFINE_string('master', '',
                    'Name of the TensorFlow master to use.')
flags.DEFINE_string('style_coefficients', None,
                    'Scales the style weights conditioned on the style image.')
flags.DEFINE_string('style_dataset_file', None, 'Style dataset file.')
flags.DEFINE_string('style_weights', DEFAULT_STYLE_WEIGHTS, 'Style weights')
flags.DEFINE_string('train_dir', None,
                    'Directory for checkpoints and summaries.')
FLAGS = flags.FLAGS


def main(unused_argv=None):
  with tf.Graph().as_default():
    # Force all input processing onto CPU in order to reserve the GPU for the
    # forward inference and back-propagation.
    device = '/cpu:0' if not FLAGS.ps_tasks else '/job:worker/cpu:0'
    with tf.device(tf.train.replica_device_setter(FLAGS.ps_tasks,
                                                  worker_device=device)):
      inputs, _ = image_utils.imagenet_inputs(FLAGS.batch_size,
                                              FLAGS.image_size)
      # Load style images and select one at random (for each graph execution, a
      # new random selection occurs)
      style_images, style_labels, \
          style_gram_matrices = image_utils.style_image_inputs(
              os.path.expanduser(FLAGS.style_dataset_file),
              batch_size=FLAGS.batch_size,
              image_size=FLAGS.image_size,
              square_crop=True,
              shuffle=True)

    with tf.device(tf.train.replica_device_setter(FLAGS.ps_tasks)):
      # Process style and weight flags
      num_styles = FLAGS.num_styles
      if FLAGS.style_coefficients is None:
        style_coefficients = [1.0 for _ in range(num_styles)]
      else:
        style_coefficients = ast.literal_eval(FLAGS.style_coefficients)
      if len(style_coefficients) != num_styles:
        raise ValueError(
            'number of style coefficients differs from number of styles')
      content_weights = ast.literal_eval(FLAGS.content_weights)
      style_weights = ast.literal_eval(FLAGS.style_weights)

      # Rescale style weights dynamically based on the current style image
      style_coefficient = tf.gather(
          tf.constant(style_coefficients), style_labels)
      style_weights = dict((key, style_coefficient * style_weights[key])
                           for key in style_weights)

      # Define the model
      stylized_inputs = model.transform(
          inputs,
          alpha=FLAGS.alpha,
          normalizer_params={
              'labels': style_labels,
              'num_categories': num_styles,
              'center': True,
              'scale': True
          })

      # Compute losses.
      total_loss, loss_dict = learning.total_loss(
          inputs, stylized_inputs, style_gram_matrices, content_weights,
          style_weights)
      for key, value in loss_dict.items():
        tf.summary.scalar(key, value)

      # Adding Image summaries to the tensorboard.
      tf.summary.image('image/0_inputs', inputs, 3)
      tf.summary.image('image/1_styles', style_images, 3)
      tf.summary.image('image/2_styled_inputs', stylized_inputs, 3)

      # Set up training
      optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate)
      train_op = slim.learning.create_train_op(
          total_loss, optimizer, clip_gradient_norm=FLAGS.clip_gradient_norm,
          summarize_gradients=False)

      # Function to restore VGG16 parameters.
      init_fn_vgg = slim.assign_from_checkpoint_fn(vgg.checkpoint_file(),
                                                   slim.get_variables('vgg_16'))

      # Run training
      slim.learning.train(
          train_op=train_op,
          logdir=os.path.expanduser(FLAGS.train_dir),
          master=FLAGS.master,
          is_chief=FLAGS.task == 0,
          number_of_steps=FLAGS.train_steps,
          init_fn=init_fn_vgg,
          save_summaries_secs=FLAGS.save_summaries_secs,
          save_interval_secs=FLAGS.save_interval_secs)


def console_entry_point():
  tf.disable_v2_behavior()
  tf.app.run(main)


if __name__ == '__main__':
  console_entry_point()
