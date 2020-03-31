#!/usr/bin/env python
# encoding: utf-8
#
# Copyright SAS Institute
#
#  Licensed under the Apache License, Version 2.0 (the License);
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import os

from dlpy.model import Model
from dlpy.layers import Conv2d, BN, OutputLayer, Input, GroupConv2d, GlobalAveragePooling2D, Res, InputLayer, Reshape, Scale
from .application_utils import get_layer_options, input_layer_options
from dlpy.utils import DLPyError
from dlpy.network import extract_input_layer, extract_output_layer, extract_conv_layer
import math

# default mobilenet v2 block parameters:
_MBConv_BLOCKS_ARGS = [
 #  (expansion, out_filters, num_blocks, kernel size, stride)
    (1, 16, 1, 3, 1, 0.25),
    (6, 24, 2, 3, 2, 0.25),
    (6, 40, 2, 5, 2, 0.25),
    (6, 80, 3, 3, 2, 0.25),
    (6, 112, 3, 5, 1, 0.25),
    (6, 192, 4, 5, 2, 0.25),
    (6, 320, 1, 3, 1, 0.25)
]

def EfficientNet(conn, model_table='EfficientNet', n_classes=100, n_channels=3, width=224, height=224,
                 width_coefficient=1, depth_coefficient=1, dropout_rate=0.2, drop_connect_rate=0, depth_divisor=8,
                 activation_fn='relu', blocks_args=_MBConv_BLOCKS_ARGS,
                 offsets=(255*0.406, 255*0.456, 255*0.485), norm_stds=(255*0.225, 255*0.224, 255*0.229),
                 random_flip=None, random_crop=None, random_mutation=None):
    '''
    Generates a deep learning model with the EfficientNet architecture.
    The implementation is revised based on
    https://github.com/keras-team/keras-applications/blob/master/keras_applications/efficientnet.py

    Parameters
    ----------
    conn : CAS
        Specifies the CAS connection object.
    model_table : string or dict or CAS table, optional
        Specifies the CAS table to store the deep learning model.
    n_classes : int, optional
        Specifies the number of classes. If None is assigned, the model will
        automatically detect the number of classes based on the training set.
        Default: 1000
    n_channels : int, optional
        Specifies the number of the channels (i.e., depth) of the input layer.
        Default: 3
    width : int, optional
        Specifies the width of the input layer.
        Default: 224
    height : int, optional
        Specifies the height of the input layer.
        Default: 224
    width_coefficient: double, optional
        Specifies the scale coefficient for network width.
        Default: 1.0
    depth_coefficient: double, optional
        Specifies the scale coefficient for network depth.
        Default: 1.0
    dropout_rate: double, optional
        Specifies the dropout rate before final classifier layer.
        Default: 0.2
    drop_connect_rate: double,
        Specifies the dropout rate at skip connections.
        Default: 0.0
    depth_divisor: integer, optional
        Specifies the unit of network width.
        Default: 8
    activation_fn: string, optional
        Specifies the activation function
    blocks_args: list of dicts
         Specifies parameters to construct blocks for the efficientnet model.
    offsets : double or iter-of-doubles, optional
        Specifies an offset for each channel in the input data. The final input
        data is set after applying scaling and subtracting the specified offsets.
        Default: (255*0.406, 255*0.456, 255*0.485)
    norm_stds : double or iter-of-doubles, optional
        Specifies a standard deviation for each channel in the input data.
        The final input data is normalized with specified means and standard deviations.
        Default: (255*0.225, 255*0.224, 255*0.229)
    random_flip : string, optional
        Specifies how to flip the data in the input layer when image data is
        used. Approximately half of the input data is subject to flipping.
        Valid Values: 'h', 'hv', 'v', 'none'
    random_crop : string, optional
        Specifies how to crop the data in the input layer when image data is
        used. Images are cropped to the values that are specified in the width
        and height parameters. Only the images with one or both dimensions
        that are larger than those sizes are cropped.
        Valid Values: 'none', 'unique', 'randomresized', 'resizethencrop'
    random_mutation : string, optional
        Specifies how to apply data augmentations/mutations to the data in the input layer.
        Valid Values: 'none', 'random'


    Returns
    -------
    :class:`Model`

    References
    ----------
    https://arxiv.org/pdf/1905.11946.pdf

    '''

    def _make_divisible(v, divisor, min_value=None):
        # make number of channel divisible
        if min_value is None:
            min_value = divisor
        new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
        # make sure that round down does not go down by more than 10%.
        if new_v < 0.9 * v:
            new_v += divisor
        return new_v

    def round_filters(filters, width_coefficient, depth_divisor):
        # round number of filters based on width multiplier and depth_divisor

        filters *= width_coefficient
        new_filters = int(filters + depth_divisor / 2) // depth_divisor * depth_divisor
        new_filters = max(depth_divisor, new_filters)
        # Make sure that round down does not go down by more than 10%.
        if new_filters < 0.9 * filters:
            new_filters += depth_divisor
        return int(new_filters)

    def round_repeats(repeats, depth_coefficient):
        # round number of repeats based on depth multiplier
        return int(math.ceil(depth_coefficient * repeats))

    def _MBConvBlock(inputs, in_channels, out_channels, ksize, stride, expansion, se_ratio, stage_id, block_id,
                     noskip=False, activation_fn='relu'):
        """
        Inverted Residual Block

        Parameters
        ----------
        inputs:
            Input tensor
        in_channels:
            Specifies the number of input tensor's channel
        out_channels:
            Specifies the number of output tensor's channel
        ksize:
            Specifies the kernel size of the convolution
        stride:
            the strides of the convolution
        expansion:
            Specifies the expansion factor for the input layer.
        se_ratio:
            Specifies the ratio to squeeze the input filters for squeeze-and-excitation block.
        stage_id:
            stage id used for naming layers
        block_id:
            block id used for naming layers
        noskip:
            Specifies whether the skip connection is used. By default, the skip connection is used.
        activation_fn:
            Specifies activation function
        """

        # mobilenetv2 block is also known as inverted residual block, which consists of three convolutions:
        # the first is 1*1 convolution for expansion
        # the second is depthwise convolution
        # the third is 1*1 convolution without any non-linearity for projection

        x = inputs
        prefix = 'stage_{}_block_{}'.format(stage_id, block_id)
        n_groups = in_channels  # for expansion=1, n_groups might be different from pointwise_filters

        if expansion > 1:
            # For MobileNet V2, expansion>1 when stage>0
            n_groups = int(expansion * in_channels)  ## update n_groups
            x = Conv2d(n_groups, 1, include_bias=False, act='identity',
                       name=prefix + 'expand')(x)
            x = BN(name=prefix + 'expand_BN', act='identity')(x)

        # Depthwise convolution with kernel size : 3 or 5
        x = GroupConv2d(n_groups, n_groups, ksize, stride=stride, act='identity',
                        include_bias=False, name=prefix + 'depthwise')(x)
        x = BN(name=prefix + 'depthwise_BN', act=activation_fn)(x)

        # Squeeze-Excitation
        if 0 < se_ratio <= 1:
            se_input = x  # features to be squeezed
            x = GlobalAveragePooling2D(name=prefix + "global_avg_pool")(x)
            # Squeeze
            channels_se = max(1, int(in_channels * se_ratio))
            x = Conv2d(channels_se, 1, include_bias=True, act=activation_fn, name=prefix + 'squeeze')(x)
            x = Conv2d(n_groups, 1, include_bias=True, act='sigmoid', name=prefix + 'excitation')(x)
            x = Reshape(name=prefix + 'reshape', width=n_groups, height=1, depth=1)(x)
            x = Scale(name=prefix + 'scale')([se_input, x])  # x = out*w

        # Project
        x = Conv2d(out_channels, 1, include_bias=False, act='identity', name=prefix + 'project')(x)
        x = BN(name=prefix + 'project_BN', act='identity')(x)  # identity activation on narrow tensor
        # Prepare output for MBConv block
        if in_channels == out_channels and stride == 1 and (not noskip):
            # dropout can be added.
            return Res(name=prefix + 'add_se_residual')([x, inputs])
        else:
            return x

    parameters = locals()
    input_parameters = get_layer_options(input_layer_options, parameters)
    inp = Input(**input_parameters, name='data')
    # refer to Table 1  "EfficientNet-B0 baseline network" in paper:
    # "EfficientNet: Rethinking Model Scaling for Convolutional Neural Networks"
    stage_id = 0
    out_channels = round_filters(32, width_coefficient,
                                 depth_divisor)  # multiply with width multiplier: width_coefficient
    x = Conv2d(out_channels, 3, stride=2, include_bias=False, name='Conv1', act='identity')(inp)
    x = BN(name='bn_Conv1', act=activation_fn)(x)

    # Create stages with MBConv blocks from stage 1
    in_channels = out_channels  # number of input channels for first MBblock
    stage_id +=1
    total_blocks = float(sum(args[2] for args in blocks_args))
    for expansion, out_channels, num_blocks, ksize, stride, se_ratio in blocks_args:
        out_channels = round_filters(out_channels, width_coefficient, depth_divisor)
        num_blocks = round_repeats(num_blocks, depth_coefficient)
        strides = [stride] + [1] * (num_blocks - 1)
        for block_id, stride in enumerate(strides):
            x = _MBConvBlock(x, in_channels, out_channels, ksize, stride, expansion, se_ratio, stage_id, block_id,activation_fn)
            in_channels = out_channels  # out_channel
        stage_id += 1

    last_block_filters = round_filters(1280, width_coefficient, depth_divisor)
    x = Conv2d(last_block_filters, 1, include_bias=False, name='Conv_top', act='identity')(x)
    x = BN(name='Conv_top_bn', act=activation_fn)(x)

    x = GlobalAveragePooling2D(name="Global_avg_pool", dropout=dropout_rate)(x)
    x = OutputLayer(n=n_classes)(x)

    model = Model(conn, inp, x, model_table)
    model.compile()
    return model


def EfficientNetB0(conn, model_table='EfficientNetB0',
                   n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=224, height=224, width_coefficient=1, depth_coefficient=1, dropout_rate=0.2,
                        **kwargs)

def EfficientNetB1(conn, model_table='EfficientNetB1', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=240, height=240, width_coefficient=1.0, depth_coefficient=1.1, dropout_rate=0.2,
                        **kwargs)

def EfficientNetB2(conn, model_table='EfficientNetB2', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=260, height=260, width_coefficient=1.1, depth_coefficient=1.2, dropout_rate=0.3,
                        **kwargs)

def EfficientNetB3(conn, model_table='EfficientNetB3', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=300, height=300, width_coefficient=1.2, depth_coefficient=1.4, dropout_rate=0.3,
                        **kwargs)

def EfficientNetB4(conn, model_table='EfficientNetB4', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=380, height=380, width_coefficient=1.4, depth_coefficient=1.8, dropout_rate=0.4,
                        **kwargs)

def EfficientNetB5(conn, model_table='EfficientNetB5', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=456, height=456, width_coefficient=1.6, depth_coefficient=2.2, dropout_rate=0.4,
                        **kwargs)

def EfficientNetB6(conn, model_table='EfficientNetB6', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=528, height=528, width_coefficient=1.8, depth_coefficient=2.6, dropout_rate=0.5,
                        **kwargs)

def EfficientNetB7(conn, model_table='EfficientNetB7', n_classes=1000,
                   **kwargs):
    return EfficientNet(conn, model_table, n_classes, n_channels=3,
                        width=600, height=600, width_coefficient=2.0, depth_coefficient=3.1, dropout_rate=0.5,
                    **kwargs)

