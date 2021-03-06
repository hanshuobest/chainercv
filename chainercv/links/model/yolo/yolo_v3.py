from __future__ import division

import itertools
import numpy as np

import chainer
from chainer.backends import cuda
import chainer.functions as F
from chainer.links import Convolution2D

from chainercv.links import Conv2DBNActiv
from chainercv import transforms
from chainercv import utils

from chainercv.links.model.ssd.ssd_vgg16 import _check_pretrained_model


def _leaky_relu(x):
    return F.leaky_relu(x, slope=0.1)


def _upsample(x):
    return F.unpooling_2d(x, 2, cover_all=False)


class ResidualBlock(chainer.ChainList):
    """ChainList with a residual connection."""

    def __init__(self, *links):
        super(ResidualBlock, self).__init__(*links)

    def __call__(self, x):
        h = x
        for link in self:
            h = link(h)
        h += x
        return h


class Darknet53Extractor(chainer.ChainList):
    """A Darknet53 based feature extractor for YOLOv3.

    This is a feature extractor for :class:`~chainercv.links.model.yolo.YOLOv3`
    """

    insize = 416
    grids = (13, 26, 52)

    def __init__(self):
        super(Darknet53Extractor, self).__init__()

        # Darknet53
        self.append(Conv2DBNActiv(32, 3, pad=1, activ=_leaky_relu))
        for k, n_block in enumerate((1, 2, 8, 8, 4)):
            self.append(Conv2DBNActiv(
                32 << (k + 1), 3, stride=2, pad=1, activ=_leaky_relu))
            for _ in range(n_block):
                self.append(ResidualBlock(
                    Conv2DBNActiv(32 << k, 1, activ=_leaky_relu),
                    Conv2DBNActiv(32 << (k + 1), 3, pad=1, activ=_leaky_relu)))

        # additional links
        for i, n in enumerate((512, 256, 128)):
            if i > 0:
                self.append(Conv2DBNActiv(n, 1, activ=_leaky_relu))
            self.append(Conv2DBNActiv(n, 1, activ=_leaky_relu))
            self.append(Conv2DBNActiv(n * 2, 3, pad=1, activ=_leaky_relu))
            self.append(Conv2DBNActiv(n, 1, activ=_leaky_relu))
            self.append(Conv2DBNActiv(n * 2, 3, pad=1, activ=_leaky_relu))
            self.append(Conv2DBNActiv(n, 1, activ=_leaky_relu))

    def __call__(self, x):
        """Compute feature maps from a batch of images.

        This method extracts feature maps from 3 layers.

        Args:
            x (ndarray): An array holding a batch of images.
                The images should be resized to :math:`416\\times 416`.

        Returns:
            list of Variable:
            Each variable contains a feature map.
        """

        ys = []
        h = x
        hs = []
        for i, link in enumerate(self):
            h = link(h)
            if i in {33, 39, 45}:
                ys.append(h)
            elif i in {14, 23}:
                hs.append(h)
            elif i in {34, 40}:
                h = F.concat((_upsample(h), hs.pop()))
        return ys


class YOLOv3(chainer.Chain):
    """YOLOv3.

    This is a model of YOLOv3 [#]_.
    This model uses :class:`~chainercv.links.model.yolo.Darknet53Extractor` as
    its feature extractor.

    .. [#] Joseph Redmon, Ali Farhadi.
       YOLOv3: An Incremental Improvement. arXiv 2018.

    Args:
       n_fg_class (int): The number of classes excluding the background.
       pretrained_model (str): The weight file to be loaded.
           This can take :obj:`'voc0712'`, `filepath` or :obj:`None`.
           The default value is :obj:`None`.

            * :obj:`'voc0712'`: Load weights trained on trainval split of \
                PASCAL VOC 2007 and 2012. \
                The weight file is downloaded and cached automatically. \
                :obj:`n_fg_class` must be :obj:`20` or :obj:`None`. \
                These weights were converted from the darknet model. \
                The conversion code is \
                `chainercv/examples/yolo/darknet2npz.py`.
            * `filepath`: A path of npz file. In this case, :obj:`n_fg_class` \
                must be specified properly.
            * :obj:`None`: Do not load weights.

    """

    _models = {
        'voc0712': {
            'n_fg_class': 20,
            'url': 'https://github.com/yuyu2172/share-weights/releases/'
            'download/0.0.6/yolo_v3_voc0712_2018_05_01.npz'
        },
    }

    _anchors = (
        ((90, 116), (198, 156), (326, 373)),
        ((61, 30), (45, 62), (119, 59)),
        ((13, 10), (30, 16), (23, 33)))

    def __init__(self, n_fg_class=None, pretrained_model=None):
        super(YOLOv3, self).__init__()

        n_fg_class, path = _check_pretrained_model(
            n_fg_class, pretrained_model, self._models)

        self.n_fg_class = n_fg_class
        self.use_preset('visualize')

        with self.init_scope():
            self.extractor = Darknet53Extractor()
            self.subnet = chainer.ChainList()

        for i, n in enumerate((512, 256, 128)):
            self.subnet.append(chainer.Sequential(
                Conv2DBNActiv(n * 2, 3, pad=1, activ=_leaky_relu),
                Convolution2D(
                    len(self._anchors[i]) * (4 + 1 + self.n_fg_class), 1)))

        default_bbox = []
        step = []
        for k, grid in enumerate(self.extractor.grids):
            for v, u in itertools.product(range(grid), repeat=2):
                for h, w in self._anchors[k]:
                    default_bbox.append((v, u, h, w))
                    step.append(self.insize / grid)
        self._default_bbox = np.array(default_bbox, dtype=np.float32)
        self._step = np.array(step, dtype=np.float32)

        if path:
            chainer.serializers.load_npz(path, self, strict=False)

    @property
    def insize(self):
        return self.extractor.insize

    def to_cpu(self):
        super(YOLOv3, self).to_cpu()
        self._default_bbox = cuda.to_cpu(self._default_bbox)
        self._step = cuda.to_cpu(self._step)

    def to_gpu(self, device=None):
        super(YOLOv3, self).to_gpu(device)
        self._default_bbox = cuda.to_gpu(self._default_bbox, device)
        self._step = cuda.to_gpu(self._step, device)

    def __call__(self, x):
        """Compute localization and classification from a batch of images.

        This method computes a variable.
        :func:`self._decode` converts this variable to bounding box
        coordinates and confidence scores.
        This variable is also used in training YOLOv3.

        Args:
            x (chainer.Variable): A variable holding a batch of images.
                The images are preprocessed by :meth:`_prepare`.

        Returns:
            chainer.Variable:
            A variable of float arrays of shape
            :math:`(B, K, 4 + 1 + n\_fg\_class)`,
            where :math:`B` is the number of samples in the batch and
            :math:`K` is the number of default bounding boxes.
        """

        ys = []
        for i, h in enumerate(self.extractor(x)):
            h = self.subnet[i](h)
            h = F.transpose(h, (0, 2, 3, 1))
            h = F.reshape(h, (h.shape[0], -1, 4 + 1 + self.n_fg_class))
            ys.append(h)
        return F.concat(ys)

    def use_preset(self, preset):
        """Use the given preset during prediction.

        This method changes values of :obj:`nms_thresh` and
        :obj:`score_thresh`. These values are a threshold value
        used for non maximum suppression and a threshold value
        to discard low confidence proposals in :meth:`predict`,
        respectively.

        If the attributes need to be changed to something
        other than the values provided in the presets, please modify
        them by directly accessing the public attributes.

        Args:
            preset ({'visualize', 'evaluate'}): A string to determine the
                preset to use.
        """

        if preset == 'visualize':
            self.nms_thresh = 0.45
            self.score_thresh = 0.5
        elif preset == 'evaluate':
            self.nms_thresh = 0.45
            self.score_thresh = 0.005
        else:
            raise ValueError('preset must be visualize or evaluate')

    def _decode(self, loc, conf):
        raw_bbox = self._default_bbox.copy()
        raw_bbox[:, :2] += 1 / (1 + self.xp.exp(-loc[:, :2]))
        raw_bbox[:, :2] *= self._step[:, None]
        raw_bbox[:, 2:] *= self.xp.exp(loc[:, 2:])
        raw_bbox[:, :2] -= raw_bbox[:, 2:] / 2
        raw_bbox[:, 2:] += raw_bbox[:, :2]

        conf = 1 / (1 + self.xp.exp(-conf))
        raw_score = conf[:, 0, None] * conf[:, 1:]

        bbox = []
        label = []
        score = []
        for l in range(self.n_fg_class):
            bbox_l = raw_bbox
            score_l = raw_score[:, l]

            mask = score_l >= self.score_thresh
            bbox_l = bbox_l[mask]
            score_l = score_l[mask]

            indices = utils.non_maximum_suppression(
                bbox_l, self.nms_thresh, score_l)
            bbox_l = bbox_l[indices]
            score_l = score_l[indices]

            bbox.append(cuda.to_cpu(bbox_l))
            label.append(np.array((l,) * len(bbox_l)))
            score.append(cuda.to_cpu(score_l))

        bbox = np.vstack(bbox).astype(np.float32)
        label = np.hstack(label).astype(np.int32)
        score = np.hstack(score).astype(np.float32)

        return bbox, label, score

    def predict(self, imgs):
        """Detect objects from images.

        This method predicts objects for each image.

        Args:
            imgs (iterable of numpy.ndarray): Arrays holding images.
                All images are in CHW and RGB format
                and the range of their value is :math:`[0, 255]`.

        Returns:
           tuple of lists:
           This method returns a tuple of three lists,
           :obj:`(bboxes, labels, scores)`.

           * **bboxes**: A list of float arrays of shape :math:`(R, 4)`, \
               where :math:`R` is the number of bounding boxes in a image. \
               Each bouding box is organized by \
               :math:`(y_{min}, x_{min}, y_{max}, x_{max})` \
               in the second axis.
           * **labels** : A list of integer arrays of shape :math:`(R,)`. \
               Each value indicates the class of the bounding box. \
               Values are in range :math:`[0, L - 1]`, where :math:`L` is the \
               number of the foreground classes.
           * **scores** : A list of float arrays of shape :math:`(R,)`. \
               Each value indicates how confident the prediction is.

        """

        x = []
        params = []
        for img in imgs:
            _, H, W = img.shape
            img, param = transforms.resize_contain(
                img / 255, (self.insize, self.insize), fill=0.5,
                return_param=True)
            x.append(self.xp.array(img.astype(np.float32)))
            param['size'] = (H, W)
            params.append(param)

        with chainer.using_config('train', False), \
                chainer.function.no_backprop_mode():
            y = self(self.xp.stack(x)).array
        locs = y[:, :, :4]
        confs = y[:, :, 4:]

        bboxes = []
        labels = []
        scores = []
        for loc, conf, param in zip(locs, confs, params):
            bbox, label, score = self._decode(loc, conf)

            bbox = transforms.translate_bbox(
                bbox, -self.insize / 2, -self.insize / 2)
            bbox = transforms.resize_bbox(
                bbox, param['scaled_size'], param['size'])
            bbox = transforms.translate_bbox(
                bbox, param['size'][0] / 2, param['size'][1] / 2)

            bboxes.append(bbox)
            labels.append(label)
            scores.append(score)

        return bboxes, labels, scores
