# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
nn
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging

import numpy as np
from onnx import onnx_pb
from onnx.onnx_pb import TensorProto
from tf2onnx import constants, utils
from tf2onnx.graph_builder import GraphBuilder
from tf2onnx.handler import tf_op
from tf2onnx.onnx_opset import common, controlflow, tensor

logger = logging.getLogger(__name__)


# pylint: disable=unused-argument,missing-docstring,unused-variable

def spatial_map(shape, perm):
    new_shape = shape[:]
    for i in perm:
        new_shape[i] = shape[perm[i]]
    return new_shape


def conv_convert_inputs(ctx, node, with_kernel=False, new_kernel_shape=None,
                        input_indices=None, output_indices=None):
    """Convert input and kernel from tensorflow to onnx. This maybe require to
        to insert transpose ops for input, kernel and output unless they are constants
        and we can transpose the constant.
        We transpose inputs if they are in NHWC. We always transpose the kernel from
        HWNC to NCHW. Outputs are transposed if the format is NHWC.
        Some convolutions like depthwise_conv2d require a reshape of the kernel.
        Args:
            ctx: the parent graph
            node: node of the convolution op
            with_kernel: transpose the kernel
            new_kernel_shape: reshape the kernel
    """

    if input_indices is None:
        input_indices = [0]
    if output_indices is None:
        output_indices = [0]

    if node.is_nhwc():
        # transpose input if needed, no need to record shapes on input
        for idx in input_indices:
            parent = node.inputs[idx]
            if node.inputs[idx].is_const() and len(ctx.find_output_consumers(node.input[1])) == 1:
                # if input is a constant, transpose that one if we are the only consumer
                val = parent.get_tensor_value(as_list=False)
                parent.set_tensor_value(val.transpose(constants.NHWC_TO_NCHW))
            else:
                # if input comes from a op, insert transpose op
                input_name = node.input[idx]
                transpose = ctx.insert_new_node_on_input(node, "Transpose", input_name)
                transpose.set_attr("perm", constants.NHWC_TO_NCHW)
                transpose.skip_conversion = True
                shape = ctx.get_shape(input_name)
                if shape is not None:
                    new_shape = spatial_map(shape, constants.NHWC_TO_NCHW)
                    ctx.set_shape(transpose.output[0], new_shape)

    # kernel must to be transposed
    if with_kernel:
        parent = node.inputs[1]
        need_transpose = True
        if node.inputs[1].is_const():
            # kernel is const - transpose the const if we are the only consumer of const
            consumers = ctx.find_output_consumers(node.input[1])
            if len(consumers) == 1:
                val = parent.get_tensor_value(as_list=False)
                val = val.transpose(constants.HWCN_TO_NCHW)
                parent.set_tensor_value(val)
                need_transpose = False

        if need_transpose:
            input_name = node.input[1]
            transpose = ctx.insert_new_node_on_input(node, "Transpose", input_name)
            transpose.set_attr("perm", constants.HWCN_TO_NCHW)
            transpose.skip_conversion = True
            new_shape = spatial_map(ctx.get_shape(input_name), constants.HWCN_TO_NCHW)
            ctx.set_shape(transpose.output[0], new_shape)

        # some onnx conv ops require the reshape the kernel (ie. depthwise_conv2d)
        if new_kernel_shape:
            if ctx.opset < 5:
                # old reshape takes new shape as attribute
                input_name = node.input[1]
                reshape = ctx.insert_new_node_on_input(node, "Reshape", input_name)
                reshape.set_attr("shape", new_kernel_shape)
                reshape.skip_conversion = True
            else:
                # new reshape takes new shape as input[1]
                shape_name = utils.make_name(node.name)
                ctx.make_const(shape_name, np.array(new_kernel_shape, dtype=np.int64))
                input_name = node.input[1]
                reshape = ctx.make_node("Reshape", [input_name, shape_name])
                ctx.replace_input(node, input_name, reshape.output[0])
                reshape.skip_conversion = True
            ctx.set_shape(reshape.output[0], new_kernel_shape)

    # transpose outputs if needed
    if node.is_nhwc():
        for idx in output_indices:
            output_name = node.output[idx]
            output_shape = ctx.get_shape(node.output[idx])
            op_name = utils.make_name(node.name)
            transpose = ctx.insert_new_node_on_output("Transpose", output_name, name=op_name)
            transpose.set_attr("perm", constants.NCHW_TO_NHWC)
            transpose.skip_conversion = True
            # set TF NHWC shape to transpose node output
            ctx.set_shape(transpose.output[0], output_shape)
            # Transpose TF NHWC shape back to NCHW shape for current ONNX conv node output
            ctx.set_shape(output_name, spatial_map(output_shape, constants.NHWC_TO_NCHW))
        node.data_format = "NCHW"


def add_padding(ctx, node, kernel_shape, strides, dilations=None, spatial=2):
    padding = node.get_attr("padding")
    if padding:
        if dilations is None:
            dilations = [1] * spatial * 2
        padding = padding.s.decode("utf-8")
        if padding == 'SAME':
            pads = [0] * spatial * 2
            input_shape = ctx.get_shape(node.input[0])
            output_shape = ctx.get_shape(node.output[0])
            # check if the input shape is valid
            if len(input_shape) != len(pads):
                logger.error("node %s input needs to be rank %d, is %d", node.name, len(pads), len(input_shape))
            # transpose shape to nchw
            if node.is_nhwc():
                input_shape = spatial_map(input_shape, constants.NHWC_TO_NCHW)
                output_shape = spatial_map(output_shape, constants.NHWC_TO_NCHW)
            # calculate pads
            if any(input_shape[i + 2] == -1 or output_shape[i + 2] == -1 for i in range(spatial)):
                logger.debug(
                    "node %s has unknown dim for pads calculation, fallback to auto_pad: "
                    "input_shape=%s, output_shape=%s",
                    node.name, input_shape, output_shape)
                node.set_attr("auto_pad", "SAME_UPPER")
            else:
                for i in range(spatial):
                    pad = (output_shape[i + 2] - 1) * strides[i] + dilations[i] * kernel_shape[i] - input_shape[i + 2]
                    pad = max(pad, 0)
                    pads[i] = pad // 2
                    pads[i + spatial] = pad - pad // 2
                node.set_attr("pads", pads)

        elif padding == 'VALID':
            pass
        else:
            raise ValueError("invalid padding value: " + padding)


def conv_dims_attr(node, name, new_name=None):
    if new_name is None:
        new_name = name
    dims = node.get_attr(name)
    if not dims:
        return None
    dims = dims.ints
    if node.is_nhwc():
        if len(dims) == 2:
            h, w = dims
            c = n = 1
        else:
            n, h, w, c = dims
    else:
        n, c, h, w = dims
    dims = [h, w]
    node.set_attr(new_name, dims)
    return dims


def conv_kernel_shape(ctx, node, input_idx, spatial=2):
    kernel_shape = ctx.get_shape(node.input[input_idx])
    if len(kernel_shape) != 2 * spatial:
        raise ValueError("kernel rank must be 2* spatial")
    kernel_shape = kernel_shape[0:spatial]
    node.set_attr("kernel_shape", kernel_shape)
    return kernel_shape


@tf_op(["Conv1D", "Conv2D", "Conv3D"])
class ConvOp:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        # T output = Conv2D(T input, T filter, @list(int) strides, @bool use_cudnn_on_gpu,
        #                       @string padding, @string data_format)
        # T Y = Conv(T X, T W, T B, @AttrType.STRING auto_pad, @AttrType.INTS dilations, @AttrType.INT group,
        #                       @AttrType.INTS kernel_shape, @AttrType.INTS pads, @AttrType.INTS strides)
        node.type = "Conv"
        kernel_shape = conv_kernel_shape(ctx, node, 1, spatial=2)
        strides = conv_dims_attr(node, "strides")
        dilations = conv_dims_attr(node, "dilations")
        add_padding(ctx, node, kernel_shape, strides, dilations=dilations, spatial=2)
        conv_convert_inputs(ctx, node, with_kernel=True)


@tf_op("Conv2DBackpropInput")
class ConvTranspose:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        # T output = Conv2DBackpropInput(int32 input_sizes, T filter, T out_backprop,
        #    @list(int) strides, @bool use_cudnn_on_gpu, @string padding, @string data_format, @list(int) dilations)
        # T Y = ConvTranspose(T X, T W, T B, @STRING auto_pad, @INTS dilations,
        #    @INT group, @INTS kernel_shape, @INTS output_shape, @INTS pads, @INTS strides)

        node.type = "ConvTranspose"
        # Note: inputs are reversed from what one would expect.
        kernel_shape = conv_kernel_shape(ctx, node, 1)

        # ouput_shape is explicitly specified here, in this case pads values are auto generated/calculated.
        output_shape = ctx.get_shape(node.output[0])
        if node.is_nhwc():
            new_output_shape = [output_shape[1], output_shape[2]]
        else:
            new_output_shape = [output_shape[2], output_shape[3]]
        node.set_attr("output_shape", new_output_shape)

        strides = conv_dims_attr(node, "strides")
        conv_dims_attr(node, "dilations")

        # remove output_shapes input
        ctx.remove_input(node, node.input[0])
        # swap data and kernel
        t = node.input[0]
        node.input[0] = node.input[1]
        node.input[1] = t

        conv_convert_inputs(ctx, node, with_kernel=True)


@tf_op(["DepthwiseConv2d", "DepthwiseConv2dNative"])
class DepthwiseConv2d:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        # T output = DepthwiseConv2dNative(T input, T filter, @list(int) strides, @string padding, @string data_format)
        # T Y = ConvTranspose(T X, T W, T B, @AttrType.STRING auto_pad, @AttrType.INTS dilations, @AttrType.INT group,
        #        @AttrType.INTS kernel_shape, @AttrType.INTS output_shape, @AttrType.INTS pads, @AttrType.INTS strides)
        #
        # this is not documented well in onnx, the hint comes from pytorch documentation:
        # http://pytorch.org/docs/master/nn.html#torch.nn.Conv2d
        #   The configuration when groups == in_channels and out_channels = K * in_channels
        #   where K is a positive integer is termed in literature as depthwise convolution.
        #   In other words, for an input of size (N,Cin,Hin,Win),
        #   if you want a depthwise convolution with a depthwise multiplier K,
        #   then you use the constructor arguments (in_channels=Cin,out_channels=Cin*K,...,groups=Cin)
        #
        node.type = "Conv"
        input_shape = ctx.get_shape(node.input[0])
        if len(input_shape) != 4:
            raise ValueError("only Conv2D is supported")

        if node.is_nhwc():
            i_n, i_h, i_w, i_c = input_shape
        else:
            i_n, i_c, i_h, i_w = input_shape

        kernel_shape = ctx.get_shape(node.input[1])
        if len(kernel_shape) != 4:
            raise ValueError("only Conv2D is supported")
        k_h, k_w, k_input_channels, k_channel_multiplier = kernel_shape
        k_output_channels = i_c * k_channel_multiplier

        node.set_attr("kernel_shape", [k_h, k_w])
        strides = conv_dims_attr(node, "strides")
        conv_dims_attr(node, "dilations")
        node.set_attr("group", i_c)
        add_padding(ctx, node, kernel_shape, strides)

        new_kernel_shape = [k_output_channels, 1, k_h, k_w]
        conv_convert_inputs(ctx, node, with_kernel=True, new_kernel_shape=new_kernel_shape)


@tf_op(["AvgPool", "AvgPool3D"], onnx_op="AveragePool")
@tf_op(["MaxPool", "MaxPoolV2"], onnx_op="MaxPool")
class PoolOp:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        cls._convert(ctx, node, **kwargs)

    @classmethod
    def version_10(cls, ctx, node, **kwargs):
        cls._convert(ctx, node, **kwargs)

    @classmethod
    def _convert(cls, ctx, node, **kwargs):
        # T output = MaxPool(T input, @list(int) ksize, @list(int) strides, @string padding, @string data_format)
        # T Y = MaxPool(T X, @AttrType.STRING auto_pad, @AttrType.INTS kernel_shape, @AttrType.INTS pads,
        #               @AttrType.INTS strides)
        # above seems wrong - input[1] is ksize, input[2] is strides
        # stride and ksize in tf is not always NHWC, so watch out when converting into onnx's NCHW
        if len(node.input) < 3:
            kernel_shape_tf = node.get_attr("ksize").ints
            strides_tf = node.get_attr("strides").ints
        else:
            kernel_shape_tf = node.inputs[1].get_tensor_value()
            strides_tf = node.inputs[2].get_tensor_value()
            ctx.remove_input(node, node.input[2])
            ctx.remove_input(node, node.input[1])

        if node.is_nhwc():
            kernel_shape_hw = kernel_shape_tf[1:3]
            strides_hw = strides_tf[1:3]
        else:
            kernel_shape_hw = kernel_shape_tf[2:4]
            strides_hw = strides_tf[2:4]
        node.set_attr("kernel_shape", kernel_shape_hw)
        node.set_attr("strides", strides_hw)
        conv_dims_attr(node, "dilations")
        add_padding(ctx, node, kernel_shape_hw, strides_hw)
        conv_convert_inputs(ctx, node, with_kernel=False)


@tf_op(["MaxPoolWithArgmax"], onnx_op="MaxPool")
class MaxPoolWithArgmaxOp:
    @classmethod
    def version_8(cls, ctx, node, **kwargs):
        # T output = MaxPool(T input, @list(int) ksize, @list(int) strides, @string padding, @string data_format)

        # Set kernel_shape attribute
        kernel_shape = node.get_attr("ksize").ints
        kernel_shape = [kernel_shape[1], kernel_shape[2]]
        node.set_attr("kernel_shape", kernel_shape)

        # Set strides attribute
        strides = node.get_attr("strides").ints
        strides = [strides[1], strides[2]]
        node.set_attr("strides", strides)

        # The input data_format is NHWC for TF MaxPoolWithArgmax
        node.set_attr("data_format", "NHWC")

        add_padding(ctx, node, kernel_shape, strides)
        conv_convert_inputs(ctx, node, with_kernel=False, input_indices=[0], output_indices=[0, 1])


@tf_op(["BiasAdd", "BiasAddV1"])
class BiasAdd:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        # T output = BiasAdd(T value, T bias, @string data_format)
        # T output = BiasAddV1(T value, T bias)
        # TODO: for now use add. We may need to convert to NCHW.
        node.type = "Add"
        common.BroadcastOp.version_1(ctx, node, **kwargs)

    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        # T output = BiasAdd(T value, T bias, @string data_format)
        # T output = BiasAddV1(T value, T bias)
        # According TF bias_add definition, the input dim is always only 1.
        node.type = "Add"
        common.BroadcastOp.version_6(ctx, node, **kwargs)

        # on NHWC, bias will broadcast from largest dim, which is default onnx Add op broadcast behavior.
        if not node.is_nhwc():
            # however, in NCHW, bias should be at 2nd dim, which by default onnx Add op has no way to know,
            # so it needs being reshaped into 3-dim tensor before add
            shape0 = ctx.get_shape(node.input[0])
            shape1 = ctx.get_shape(node.input[1])
            if node.inputs[1].type == 'Const' and len(shape1) == 1:
                new_broadcast_shape = [shape1[0]] + [1] * (len(shape0) - 2)
                shape_name = utils.make_name(node.name)
                ctx.make_const(shape_name, np.array(new_broadcast_shape, dtype=np.int64))
                op_name = node.input[1]
                reshape_node = ctx.make_node("Reshape", [op_name, shape_name])
                ctx.replace_input(node, op_name, reshape_node.output[0])
                ctx.set_shape(reshape_node.output[0], new_broadcast_shape)


@tf_op(["Pad", "PadV2", "MirrorPad"])
class Pad:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        node.type = "Pad"
        # T output = Pad(T input, int32 paddings, @type Tpaddings), CONST model using default value
        #  or PadV2(T input, int32 paddings, T constant_value, @type Tpaddings), CONST mode - default value specified
        #  or MirrorPad(T input, int32 paddings, @type Tpaddings, @STRING mode), other mode.
        # T output = Pad(T data, @STRING mode, @INTS pads, @FLOAT value)
        paddings = np.array(node.inputs[1].get_tensor_value()).transpose().flatten()
        mode = node.get_attr("mode")
        if mode:
            mode = mode.s.decode("utf-8").lower()
            node.set_attr("mode", mode)
        if mode not in [None, "constant", "reflect"]:
            raise ValueError(mode + " pad mode is not supported")

        if mode in [None, "constant"] and len(node.input) == 3:
            const_val = node.inputs[2].get_tensor_value()
            node.set_attr("value", const_val)
            ctx.remove_input(node, node.input[2])

        ctx.remove_input(node, node.input[1])
        node.set_attr("pads", paddings)

        origin_dtype = ctx.get_dtype(node.output[0])
        if origin_dtype not in [onnx_pb.TensorProto.FLOAT16, onnx_pb.TensorProto.FLOAT,
                                onnx_pb.TensorProto.DOUBLE]:
            cast_node = ctx.insert_new_node_on_input(node, "Cast", node.input[0])
            cast_node.set_attr("to", onnx_pb.TensorProto.FLOAT)
            ctx.set_dtype(cast_node.output[0], onnx_pb.TensorProto.FLOAT)
            ctx.copy_shape(node.name, cast_node.output[0])

            cast_back_node = ctx.insert_new_node_on_output("Cast", node.output[0],
                                                           name=utils.make_name(node.name) + "_castback")
            cast_back_node.set_attr("to", origin_dtype)
            ctx.set_dtype(cast_back_node.output[0], origin_dtype)
            ctx.copy_shape(node.name, cast_back_node.output[0])


@tf_op(["FusedBatchNorm", "FusedBatchNormV2"])
class BatchNorm:
    @classmethod
    def version_6(cls, ctx, node, **kwargs):
        node.type = "BatchNormalization"
        # tf inputs: x, scale, bias, mean, variance
        # tf outputs: y, batch_mean, batch_var
        # a: data_format, epsilon, is_training
        # onnx inputs: X, scale, B, mean, variance, attributes: epsilon, momentum=0.9, spatial : 1
        # output: y, mean, var, savedmean, savedvar,
        # detach unused outputs. While we could let the unused outputs dangle,
        # some runtimes like pytorch/caffe2 do complain about it.
        consumers = [ctx.find_output_consumers(output_name) for output_name in node.output[1:]]
        if not any(consumers):
            new_output = [node.output[0]]
            node.output = new_output

        conv_convert_inputs(ctx, node, with_kernel=False)

        scale_shape = ctx.get_shape(node.input[1])
        mean_shape = ctx.get_shape(node.input[3])
        var_shape = ctx.get_shape(node.input[4])
        val_type = utils.map_onnx_to_numpy_type(ctx.get_dtype(node.input[1]))

        if mean_shape != scale_shape:
            new_mean_value = np.array(np.resize(node.inputs[3].get_tensor_value(as_list=False), scale_shape),
                                      dtype=val_type)
            new_mean_node_name = utils.make_name(node.name)
            ctx.make_const(new_mean_node_name, new_mean_value)
            node.input[3] = new_mean_node_name

        if var_shape != scale_shape:
            new_var_value = np.array(np.resize(node.inputs[4].get_tensor_value(as_list=False), scale_shape),
                                     dtype=val_type)
            new_val_node_name = utils.make_name(node.name)
            ctx.make_const(new_val_node_name, new_var_value)
            node.input[4] = new_val_node_name

    @classmethod
    def version_9(cls, ctx, node, **kwargs):
        # is_test was removed - no change for us
        cls.version_6(ctx, node, **kwargs)


@tf_op(["SpaceToDepth", "DepthToSpace"])
class SpaceToDepth:
    @classmethod
    def version_1(cls, ctx, node, **kwargs):
        block_size = node.get_attr("block_size")
        node.set_attr("blocksize", block_size.i)
        conv_convert_inputs(ctx, node, with_kernel=False)


@tf_op(["ResizeBilinear", "ResizeNearestNeighbor"])
class Resize:
    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        mode = "linear" if node.type == "ResizeBilinear" else "nearest"
        node.type = "Upsample"
        shape = ctx.get_shape(node.input[0])
        target_shape = node.inputs[1].get_tensor_value()
        # https://www.tensorflow.org/api_docs/python/tf/image/resize_nearest_neighbor
        # wants the input to be NHWC - adjust target_shape to this.
        n, h, w, c = shape
        nh, nw = target_shape
        utils.make_sure(all(i != -1 for i in [nh, nw]), "h and w need to be known")
        # scaler is nchw
        scaler = [1., 1., float(nh) / h, float(nw) / w]
        node.set_attr("scales", scaler)
        node.set_attr("mode", mode)
        ctx.remove_input(node, node.input[1])
        node.data_format = "NHWC"
        conv_convert_inputs(ctx, node, with_kernel=False)

    @classmethod
    def version_9(cls, ctx, node, **kwargs):
        cls._convert_since_9(ctx, node, op_type="Upsample")

    @classmethod
    def version_10(cls, ctx, node, **kwargs):
        cls._convert_since_9(ctx, node, op_type="Resize")

    @classmethod
    def _convert_since_9(cls, ctx, node, op_type):

        # float32 out = ResizeBilinear/ResizeNearestNeighbor(T images, int size)
        # https://www.tensorflow.org/api_docs/python/tf/image/resize_nearest_neighbor
        # wants the input to be NHWC - adjust target_shape to this.
        mode = "linear" if node.type == "ResizeBilinear" else "nearest"

        # first create "scales" info for onnx upsample
        # if shape of input and output known then  "scale" is calculated statically and set as a const node
        shape = ctx.get_shape(node.input[0])
        if shape and shape[2] != -1 and shape[1] != -1 and node.inputs[1].is_const():
            target_shape = node.inputs[1].get_tensor_value()
            n, h, w, c = shape
            nh, nw = target_shape
            # scales is nchw
            # the reason not storing data at raw field is because of the bug: https://github.com/onnx/onnx/issues/1852
            scale_val = np.array([1.0, 1.0, float(nh) / h, float(nw) / w]).astype(np.float32)
            scales = ctx.make_const(utils.make_name("scales"), scale_val, raw=False)
        else:
            ori_shape = ctx.make_node("Shape", [node.input[0]])
            attr = {"axes": [0], "starts": [1], "ends": [3]}
            inputs_map = {"data": ori_shape.output[0], **attr}
            ori_shape_hw = GraphBuilder(ctx).make_slice(inputs_map)
            ori_shape_hw_float = ctx.make_node("Cast", [ori_shape_hw], attr={"to": onnx_pb.TensorProto.FLOAT})

            target_hw = node.inputs[1]
            target_hw_float = ctx.make_node("Cast", target_hw.output, attr={"to": onnx_pb.TensorProto.FLOAT})

            scales_hw = ctx.make_node("Div", [target_hw_float.output[0], ori_shape_hw_float.output[0]])

            const_one_array = ctx.make_const(utils.make_name("one"), np.array([1.0, 1.0]).astype(np.float32))
            # scales is nchw
            scales = ctx.make_node("Concat", [const_one_array.output[0], scales_hw.output[0]], {"axis": 0})
        # because onnxruntime only supports to scale the last two dims so transpose is inserted
        input_nchw = ctx.make_node("Transpose", [node.input[0]], {"perm": [0, 3, 1, 2]})
        upsample = ctx.make_node(op_type, [input_nchw.output[0], scales.output[0]], attr={"mode": mode})

        shapes = node.output_shapes
        dtypes = node.output_dtypes
        ctx.remove_node(node.name)
        ctx.make_node("Transpose", upsample.output, {"perm": [0, 2, 3, 1]},
                      name=node.name, outputs=node.output, shapes=shapes, dtypes=dtypes)


@tf_op("MatrixBandPart")
class MatrixBandPart:
    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        # T output = MatrixBandPart(T input, int num_lower, int num_upper)
        # data-flow: first generate mask matrix and then use element-wise mul op
        input_rank = len(ctx.get_shape(node.input[0]))
        utils.make_sure(input_rank == 2, error_msg="MatrixBandPart op: only rank 2 is supported")
        bandpart = [node.inputs[ind].get_tensor_value() for ind in [1, 2]]
        utils.make_sure(bandpart in [[-1, 0], [0, -1]], "only support Lower/Upper triangular for now")
        # methods to generate mask matrix: if lower triangular is needed, then generate column one by one
        # otherwise row is generated one by one.
        axis, counter_axis, squeeze_axis = (1, 0, 2) if bandpart == [-1, 0] else (0, 1, 1)
        # 1: subgraph to implement tf.onelike(input[:, 0]),
        # no need to worry about the dtype, because bool type is needed as Xor only support bool
        node_name = utils.make_name("const_zero")
        const_zero = ctx.make_const(name=node_name, np_val=np.array([0]).astype(np.int32))
        first_col_or_row = ctx.make_node(op_type="Gather", inputs=[node.input[0], const_zero.output[0]],
                                         attr={"axis": axis})
        first_col_or_row_casted = ctx.make_node(op_type="Cast", inputs=first_col_or_row.output,
                                                attr={"to": onnx_pb.TensorProto.BOOL})
        # line means one col or one row
        zero_line = ctx.make_node(op_type="Xor", inputs=first_col_or_row_casted.output * 2)
        one_line = ctx.make_node(op_type="Not", inputs=zero_line.output)

        # 2: "loop" to generate mask matrix: generate col or row of matrix one by one
        g = ctx.create_new_graph_with_same_config()
        node_name = utils.make_name("const_zero_bool")
        const_zero_bool = ctx.make_const(name=node_name, np_val=np.array([[0]]).astype(np.bool))
        ctx.set_dtype(const_zero_bool.output[0], onnx_pb.TensorProto.BOOL)

        # shift right the line and add zero at the left.
        new_line = g.make_node(op_type="Concat", inputs=[const_zero_bool.output[0], "line"],
                               attr={"axis": counter_axis},
                               dtypes=[onnx_pb.TensorProto.BOOL])
        attr = {"axes": [counter_axis], "starts": [0], "ends": [-1]}
        inputs_map = {"data": new_line.output[0], **attr}
        slice_node = GraphBuilder(g).make_slice(inputs_map)

        g.make_node("Identity", ["cond"], outputs=["cond_out"])
        g.make_node("Identity", ["line"], outputs=["res"])
        g.make_node("Identity", [slice_node], outputs=["line_out"])

        g.add_graph_input("trip", onnx_pb.TensorProto.INT64, [])
        g.add_graph_input("cond", onnx_pb.TensorProto.BOOL, [])
        g.add_graph_input("line", onnx_pb.TensorProto.BOOL, [-1, -1])

        g.add_graph_output("cond_out", onnx_pb.TensorProto.BOOL, [])
        g.add_graph_output("line_out", onnx_pb.TensorProto.BOOL, [-1, -1])
        g.add_graph_output("res", onnx_pb.TensorProto.BOOL, [-1, -1])

        # initial value of body vars
        shape = ctx.make_node(op_type="Shape", inputs=[node.input[0]])  # dtype of result is int64
        node_name = utils.make_name("line_num_index")
        col_or_row_num_index = ctx.make_const(name=node_name, np_val=np.array(axis).astype(np.int32))
        line_num = ctx.make_node(op_type="Gather", inputs=[shape.output[0], col_or_row_num_index.output[0]])
        trip_cnt = line_num.output[0]
        node_name = utils.make_name("true")
        cond = ctx.make_const(name=node_name, np_val=np.array(1).astype(np.bool))
        col_init = one_line.output[0]

        loop_node = ctx.make_node(op_type="Loop", inputs=[trip_cnt, cond.output[0], col_init], output_count=2)
        loop_node.set_body_graph_as_attr("body", g)
        # convert generated mask matrix from bool to right shape and data type
        squeeze = ctx.make_node(op_type="Squeeze", inputs=[loop_node.output[1]], attr={"axes": [squeeze_axis]})
        cast1 = ctx.make_node(op_type="Cast", inputs=squeeze.output, attr={"to": onnx_pb.TensorProto.FLOAT})
        if axis == 1:
            mask_matrix = ctx.make_node(op_type="Transpose", inputs=cast1.output)
        else:
            mask_matrix = squeeze
        cast2 = ctx.make_node(op_type="Cast", inputs=mask_matrix.output,
                              attr={"to": ctx.get_dtype(node.input[0])})
        shapes = node.output_shapes
        dtypes = node.output_dtypes
        ctx.remove_node(node.name)
        ctx.make_node(op_type="Mul", inputs=[cast2.output[0], node.input[0]],
                      name=node.name, outputs=node.output, shapes=shapes,
                      dtypes=dtypes)


def _make_softmax_cross_entropy_with_logits(ctx, label, logit, tf_ori_node):
    label_dtype = ctx.get_dtype(label.output[0])
    logit_dtype = ctx.get_dtype(logit.output[0])
    utils.make_sure(label_dtype == logit_dtype, "the following logic only works on same dtype of label and logit")

    log_softmax = ctx.make_node(op_type="LogSoftmax", inputs=logit.output)
    # implement tf.multiply(-1, tf.reduce_sum(tf.multiply(label, log_softmax), axis=1))
    mul1 = ctx.make_node(op_type="Mul", inputs=[label.output[0], log_softmax.output[0]])
    reduce_sum = ctx.make_node(op_type="ReduceSum", inputs=[mul1.output[0]], attr={"axes": [-1]})
    const_negative_one = ctx.make_const(name=utils.make_name("const_negative_one"),
                                        np_val=np.array(-1).astype(utils.ONNX_TO_NUMPY_DTYPE[logit_dtype]))
    mul2 = ctx.make_node(op_type="Mul", inputs=[const_negative_one.output[0], reduce_sum.output[0]])
    shapes = tf_ori_node.output_shapes
    dtypes = tf_ori_node.output_dtypes
    ctx.remove_node(tf_ori_node.name)
    ctx.make_node(op_type="Squeeze", inputs=[mul2.output[0]], attr={"axes": [1]},
                  outputs=[tf_ori_node.output[0]], shapes=[shapes[0]], dtypes=[dtypes[0]])


def sparse_softmax_cross_entropy_with_logits_op_by_gathernd(ctx, node, **kwargs):
    # make subgraph to implement one_hot, idea comes from onehot_op
    indices_name = node.input[1]
    indices_shape = ctx.get_shape(indices_name)
    if len(indices_shape) != 1:
        # TODO: this works for rank=1 but tensorflow supports more than this.
        # Same principle should work but we need to implement our own eye.
        raise ValueError("onehot op: only rank1 is supported")
    logit_name = node.input[0]
    logit_dtype = ctx.get_dtype(logit_name)
    logit_shape = ctx.get_shape(logit_name)
    utils.make_sure(logit_dtype, "Dtype of {} is None".format(logit_name))
    indices_dtype = ctx.get_dtype(indices_name)
    if indices_dtype != TensorProto.INT64:
        indices_cast = ctx.make_node("Cast", [indices_name], attr={"to": TensorProto.INT64})
        indices_name = indices_cast.output[0]
    indices_size = ctx.make_node("Size", [indices_name])
    indices_unsqueeze = ctx.make_node("Unsqueeze", [indices_name], attr={"axes": [1]})
    zero_const = ctx.make_const(utils.make_name("zero"), np.array(0, dtype=np.int64))
    one_const = ctx.make_const(utils.make_name("one"), np.array(1, dtype=np.int64))
    id_name = utils.make_name("sparse_softmax_id")
    id_output = utils.port_name(id_name)
    controlflow.make_range(ctx, zero_const.output[0], indices_size.output[0], one_const.output[0],
                           id_output, id_name, shape=[-1], dtype=TensorProto.INT64)
    id_unsqueeze = ctx.make_node("Unsqueeze", [id_output], attr={"axes": [1]})
    indices_with_id = ctx.make_node("Concat",
                                    [id_unsqueeze.output[0], indices_unsqueeze.output[0]],
                                    attr={"axis": 1})
    log_softmax = ctx.make_node(op_type="LogSoftmax",
                                inputs=[logit_name], dtypes=[logit_dtype], shapes=[logit_shape])
    gathernd_name = utils.make_name("sparse_softmax_gathernd")
    gathernd_output = utils.port_name(gathernd_name)
    tensor.make_gathernd(ctx, log_softmax.output[0], indices_with_id.output[0], gathernd_output,
                         gathernd_name, logit_dtype, [logit_shape], [logit_dtype])
    const_name = utils.make_name("const_negative_one")
    const_negative_one = ctx.make_const(const_name, np.array(-1).astype(utils.map_onnx_to_numpy_type(logit_dtype)))
    mul2 = ctx.make_node(op_type="Mul", inputs=[const_negative_one.output[0], gathernd_output])
    shapes = node.output_shapes
    dtypes = node.output_dtypes
    ctx.remove_node(node.name)
    ctx.make_node(op_type="Squeeze",
                  inputs=[mul2.output[0]], outputs=[node.output[0]],
                  attr={"axes": [1]}, shapes=[shapes[0]], dtypes=[dtypes[0]])


@tf_op("SoftmaxCrossEntropyWithLogits")
class SoftmaxCrossEntropyWithLogits:
    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        logits = node.inputs[0]
        logit_dtype = ctx.get_dtype(logits.output[0])
        labels = node.inputs[1]
        label_dtype = ctx.get_dtype(labels.output[0])
        if label_dtype != logit_dtype:
            labels = ctx.make_node("Cast", labels.output, attr={"to": logit_dtype}, dtypes=[logit_dtype])

        _make_softmax_cross_entropy_with_logits(ctx, labels, logits, node)


def _make_sparse_softmax_cross_entropy_with_logits(ctx, label, logit, tf_ori_node):
    logit = logit.output[0]
    label = label.output[0]
    label_dtype = ctx.get_dtype(label)
    logit_dtype = ctx.get_dtype(logit)
    utils.make_sure(label_dtype == logit_dtype, "the following logic only works on same dtype of label and logit")

    # when label is onehot, logic "tf.multiply(-1, tf.reduce_sum(tf.multiply(label, log_softmax), axis=1))" is equal to
    # "-log(q_i)" where i is the selected index specified by label, q_i = logic_i/sum, the detail process is as follows:
    # logit_exp=exp(logit) >> sum = tf.reduce_sum(logit_exp, axis = -1), masked_sum = reduce_sum(mul(logit_exp, mul))
    # >> -log(masked_sum/sum)
    logit_exp = ctx.make_node(op_type="Exp", inputs=[logit]).output[0]
    logit_exp_sum = ctx.make_node(op_type="ReduceSum", inputs=[logit_exp], attr={"axes": [-1], "keepdims": 0}).output[0]
    masked = ctx.make_node(op_type="Mul", inputs=[label, logit_exp]).output[0]
    masked_sum = ctx.make_node(op_type="ReduceSum", inputs=[masked], attr={"axes": [-1], "keepdims": 0}).output[0]
    probability = ctx.make_node(op_type="Div", inputs=[masked_sum, logit_exp_sum]).output[0]
    log_prob = ctx.make_node(op_type="Log", inputs=[probability]).output[0]
    const_negative_one = ctx.make_const(name=utils.make_name("const_negative_one"),
                                        np_val=np.array(-1).astype(utils.ONNX_TO_NUMPY_DTYPE[logit_dtype])).output[0]

    shapes = tf_ori_node.output_shapes
    dtypes = tf_ori_node.output_dtypes
    ctx.remove_node(tf_ori_node.name)
    res = ctx.make_node(op_type="Mul", inputs=[log_prob, const_negative_one],
                        outputs=[tf_ori_node.output[0]], shapes=[shapes[0]], dtypes=[dtypes[0]])


@tf_op("SparseSoftmaxCrossEntropyWithLogits")
class SparseSoftmaxCrossEntropyWithLogits:
    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        # make subgraph to implement one_hot, idea comes from onehot_op
        indices_name = node.input[1]
        indices_shape = ctx.get_shape(indices_name)
        if len(indices_shape) != 1:
            # TODO: this works for rank=1 but tensorflow supports more than this.
            # Same principle should work but we need to implement our own eye.
            raise ValueError("onehot op: only rank1 is supported")
        logit_name = node.input[0]
        depth = ctx.get_shape(logit_name)[-1]
        # if number of classes is unknown or too large
        if depth == utils.ONNX_UNKNOWN_DIMENSION or depth > 20000:
            sparse_softmax_cross_entropy_with_logits_op_by_gathernd(ctx, node, **kwargs)
            return
        logit_dtype = ctx.get_dtype(logit_name)
        utils.make_sure(logit_dtype, "Dtype of {} is None".format(logit_name))

        dtype = utils.map_onnx_to_numpy_type(logit_dtype)
        eye = np.eye(depth).astype(dtype)
        const_name = utils.make_name("const_eye")
        const_eye = ctx.make_const(name=const_name, np_val=eye)
        onehot = ctx.make_node(op_type="Gather", inputs=[const_eye.output[0], indices_name], attr={"axis": 0})
        log_softmax = ctx.make_node(op_type="LogSoftmax", inputs=[logit_name])
        # implement tf.multiply(np.float32(-1.0), tf.reduce_sum(tf.multiply(one_hot, log_softmax), axis=1))
        mul1 = ctx.make_node(op_type="Mul", inputs=[onehot.output[0], log_softmax.output[0]])
        reduce_sum = ctx.make_node(op_type="ReduceSum", inputs=[mul1.output[0]], attr={"axes": [1]})
        const_name = utils.make_name("const_negative_one")
        const_negative_one = ctx.make_const(name=const_name, np_val=np.array(-1).astype(dtype))
        mul2 = ctx.make_node(op_type="Mul", inputs=[const_negative_one.output[0], reduce_sum.output[0]])

        shapes = node.output_shapes
        dtypes = node.output_dtypes
        ctx.remove_node(node.name)
        ctx.make_node(op_type="Squeeze", inputs=[mul2.output[0]], outputs=[node.output[0]], attr={"axes": [1]},
                      shapes=[shapes[0]], dtypes=[dtypes[0]])

    @classmethod
    def version_9(cls, ctx, node, **kwargs):
        # float32/64 output = SparseSoftmaxCrossEntropyWithLogits(float32/64 features, int32/64 labels)
        # the detail math process of this op is: a = onehot(labels), b = logsoftmax(features), reduce_sum(mul(a, b))
        logit_node = node.inputs[0]
        logit_shape = ctx.get_shape(node.input[0])
        logit_dtype = ctx.get_dtype(node.input[0])

        label_name = node.input[1]

        if logit_shape is not None and logit_shape[-1] != -1:
            num_class = logit_shape[-1]
            node_nme = utils.make_name("onehot_depth")
            depth_node = ctx.make_const(node_nme, np.array([num_class]).astype(np.int64)).output[0]
        else:
            logit_shape = ctx.make_node("Shape", [node.input[0]]).output[0]
            slice_args = {"data": logit_shape,
                          "starts": [-1], "ends": [int(utils.get_max_value(np.int32))]}
            num_class = GraphBuilder(ctx).make_slice(kwargs=slice_args)
            depth_node = num_class
        values_node = ctx.make_const(utils.make_name("onehot_values"), np.array([0, 1]).astype(np.int64)).output[0]
        label_dtype = ctx.get_dtype(label_name)
        if label_dtype != TensorProto.INT64:
            onehot_indice = ctx.make_node("Cast", [label_name], attr={"to": TensorProto.INT64}).output[0]
        else:
            onehot_indice = label_name
        label_node = ctx.make_node(op_type="OneHot",
                                   inputs=[onehot_indice, depth_node, values_node])
        # the above logic makes output dtype of label_node now always int64
        # make sure label has same dtype as logit
        if logit_dtype != TensorProto.INT64:
            label_node = ctx.make_node("Cast", label_node.output, attr={"to": logit_dtype}, dtypes=[logit_dtype])

        _make_sparse_softmax_cross_entropy_with_logits(ctx, label_node, logit_node, node)
