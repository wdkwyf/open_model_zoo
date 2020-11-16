import json
import logging as log
import os
import pickle as pkl
import re
import subprocess
import tempfile
from enum import Enum
from multiprocessing.pool import ThreadPool

import cv2 as cv
import numpy as np
import sympy
from tqdm import tqdm
from openvino.inference_engine import IECore

START_TOKEN = 0
END_TOKEN = 2
DENSITY = 300
DEFAULT_RESOLUTION = (1280, 720)
DEFAULT_WIDTH = 800
MIN_HEIGHT = 30
MAX_HEIGHT = 150
MAX_WIDTH = 1200
MIN_WIDTH = 260
COLOR_WHITE = (255, 255, 255)
COLOR_BLACK = (0, 0, 0)
RED = (0, 0, 255)
# default value to resize input window's width in pixels
DEFAULT_RESIZE_STEP = 10


def strip_internal_spaces(text):
    """
    Removes spaces between digits, digit and dot,
    dot and digit; after opening brackets and parentheses
    and before closing ones; spaces around ^ symbol.
    """
    text = text.replace("{ ", "{")
    text = text.replace(" }", "}")
    text = text.replace("( ", "(")
    text = text.replace(" )", ")")
    while re.search(r"([\d]) ([\d])", text):
        text = re.sub(r"([\d]) ([\d])", r"\1\2", text)
    text = re.sub(r"([\d]) ([\.])", r"\1\2", text)
    text = re.sub(r"([\.]) ([\d])", r"\1\2", text)
    text = text.replace(" ^ ", "^")
    return text


def crop(img, target_shape):
    target_height, target_width = target_shape
    img_h, img_w = img.shape[0:2]
    new_w = min(target_width, img_w)
    new_h = min(target_height, img_h)
    return img[:new_h, :new_w, :]


def resize(img, target_shape):
    target_height, target_width = target_shape
    img_h, img_w = img.shape[0:2]
    scale = min(target_height / img_h, target_width / img_w)
    return cv.resize(img, None, fx=scale, fy=scale)


PREPROCESSING = {
    'crop': crop,
    'resize': resize
}


def create_renderer():
    """
    Checks if pdflatex is installed and rendering
    of latex formula could be performed
    """
    command = subprocess.run("pdflatex --version", stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False, shell=True)
    if command.returncode != 0:
        renderer = None
        log.warning("pdflatex not installed, please, install it to use rendering")
    else:
        renderer = Renderer()
    return renderer


def preprocess_image(preprocess, image_raw, tgt_shape):
    """
    Crop or resize with constant aspect ratio
    and bottom right pad resulting image
    """
    target_height, target_width = tgt_shape
    image_raw = preprocess(image_raw, tgt_shape)
    img_h, img_w = image_raw.shape[0:2]
    image_raw = cv.copyMakeBorder(image_raw, 0, target_height - img_h,
                                  0, target_width - img_w, cv.BORDER_CONSTANT,
                                  None, COLOR_WHITE)
    return image_raw


def prerocess_crop(crop, tgt_shape, preprocess_type='crop'):
    """
    Binarize image and call preprocess_image function
    """
    height, width = tgt_shape
    crop = cv.cvtColor(crop, cv.COLOR_BGR2GRAY)
    crop = cv.cvtColor(crop, cv.COLOR_GRAY2BGR)
    ret_val, bin_crop = cv.threshold(crop, 120, 255, type=cv.THRESH_BINARY)
    return preprocess_image(PREPROCESSING[preprocess_type], bin_crop, tgt_shape)


def read_net(model_xml, ie, device):
    model_bin = os.path.splitext(model_xml)[0] + ".bin"

    log.info("Loading network files:\n\t{}\n\t{}".format(model_xml, model_bin))
    model = ie.read_network(model_xml, model_bin)
    return model


def print_stats(module):
    perf_counts = module.requests[0].get_perf_counts()
    print('{:<70} {:<15} {:<15} {:<15} {:<10}'.format('name', 'layer_type', 'exet_type', 'status',
                                                      'real_time, us'))
    for layer, stats in perf_counts.items():
        print('{:<70} {:<15} {:<15} {:<15} {:<10}'.format(layer, stats['layer_type'], stats['exec_type'],
                                                          stats['status'], stats['real_time']))


def change_layout(model_input):
    """
    Change layout of the image from [H, W, C] to [N, C, H, W]
    where N is equal to one (batch dimension)
    """
    model_input = model_input.transpose((2, 0, 1))
    model_input = np.expand_dims(model_input, axis=0)
    return model_input

def calculate_probability(logits):
    prob = 1
    probabilities = np.amax(logits, axis=1)
    for p in probabilities:
        prob *= p
    return prob


class Model:
    class Status(Enum):
        ready = 0
        encoder_infer = 1
        decoder_infer = 2

    def __init__(self, args):
        self.args = args
        log.info("Creating Inference Engine")
        self.ie = IECore()
        self.ie.set_config(
            {"PERF_COUNT": "YES" if self.args.perf_counts else "NO"}, args.device)
        self.encoder = read_net(self.args.m_encoder, self.ie, self.args.device)
        self.dec_step = read_net(self.args.m_decoder, self.ie, self.args.device)
        self.exec_net_encoder = self.ie.load_network(network=self.encoder, device_name=self.args.device)
        self.exec_net_decoder = self.ie.load_network(network=self.dec_step, device_name=self.args.device)
        self.images_list = []
        self.vocab = Vocab(self.args.vocab_path)
        self.model_status = Model.Status.ready
        self.is_async = args.interactive
        self.num_infers_decoder = 0
        if not args.interactive:
            self.preprocess_inputs()

    def preprocess_inputs(self):
        batch_dim, channels, height, width = self.encoder.input_info['imgs'].input_data.shape
        assert batch_dim == 1, "Demo only works with batch size 1."
        assert channels in (1, 3), "Input image is not 1 or 3 channeled image."
        target_shape = (height, width)
        if os.path.isdir(self.args.input):
            inputs = sorted(os.path.join(self.args.input, inp)
                            for inp in os.listdir(self.args.input))
        else:
            inputs = [self.args.input]
        log.info("Loading and preprocessing images")
        for filenm in tqdm(inputs):
            image_raw = cv.imread(filenm)
            assert image_raw is not None, "Error reading image {}".format(filenm)
            image = preprocess_image(
                PREPROCESSING[self.args.preprocessing_type], image_raw, target_shape)
            record = dict(img_name=filenm, img=image, formula=None)
            self.images_list.append(record)

    def _async_infer_encoder(self, image, req_id):
        return self.exec_net_encoder.start_async(request_id=req_id, inputs={self.args.imgs_layer: image})

    def _async_infer_decoder(self, row_enc_out, dec_st_c, dec_st_h, output, tgt, req_id):
        self.num_infers_decoder += 1
        return self.exec_net_decoder.start_async(request_id=req_id, inputs={self.args.row_enc_out_layer: row_enc_out,
                                                                            self.args.dec_st_c_layer: dec_st_c,
                                                                            self.args.dec_st_h_layer: dec_st_h,
                                                                            self.args.output_prev_layer: output,
                                                                            self.args.tgt_layer: tgt
                                                                            }
                                                 )

    def infer_async(self, model_input):
        model_input = change_layout(model_input)
        assert self.is_async
        # asynchronous variant
        if self.model_status == Model.Status.ready:
            infer_status_encoder = self._run_encoder(model_input)
            return None

        if self.model_status == Model.Status.encoder_infer:
            infer_status_encoder = self._infer_request_handle_encoder.wait(timeout=1)
            if infer_status_encoder == 0:
                self._run_decoder()
            return None

        return self._process_decoding_results()

    def infer_sync(self, model_input):
        assert not self.is_async
        model_input = change_layout(model_input)
        self._run_encoder(model_input)
        self._run_decoder()
        res = None
        while res is None:
            res = self._process_decoding_results()
        return res

    def _process_decoding_results(self):
        timeout = 1 if self.is_async else -1
        infer_status_decoder = self._infer_request_handle_decoder.wait(timeout)
        if infer_status_decoder != 0 and self.is_async:
            return None
        dec_res = self._infer_request_handle_decoder.output_blobs
        self._unpack_dec_results(dec_res)

        if self.tgt[0][0][0] == END_TOKEN or self.num_infers_decoder >= self.args.max_formula_len:
            self.num_infers_decoder = 0
            self.logits = np.array(self.logits)
            logits = self.logits.squeeze(axis=1)
            targets = np.argmax(logits, axis=1)
            self.model_status = Model.Status.ready
            return logits, targets
        self._infer_request_handle_decoder = self._async_infer_decoder(self.row_enc_out,
                                                                       self.dec_states_c,
                                                                       self.dec_states_h,
                                                                       self.output,
                                                                       self.tgt,
                                                                       req_id=0
                                                                       )

        return None

    def _run_encoder(self, model_input):
        timeout = 1 if self.is_async else -1
        self._infer_request_handle_encoder = self._async_infer_encoder(model_input, req_id=0)
        self.model_status = Model.Status.encoder_infer
        infer_status_encoder = self._infer_request_handle_encoder.wait(timeout=timeout)
        return infer_status_encoder

    def _run_decoder(self):
        enc_res = self._infer_request_handle_encoder.output_blobs
        self._unpack_enc_results(enc_res)
        self._infer_request_handle_decoder = self._async_infer_decoder(
            self.row_enc_out, self.dec_states_c, self.dec_states_h, self.output, self.tgt, req_id=0)
        self.model_status = Model.Status.decoder_infer

    def _unpack_dec_results(self, dec_res):
        self.dec_states_h = dec_res[self.args.dec_st_h_t_layer].buffer
        self.dec_states_c = dec_res[self.args.dec_st_c_t_layer].buffer
        self.output = dec_res[self.args.output_layer].buffer
        logit = dec_res[self.args.logit_layer].buffer
        self.logits.append(logit)
        self.tgt = np.array([[np.argmax(logit, axis=1)]])

    def _unpack_enc_results(self, enc_res):
        self.row_enc_out = enc_res[self.args.row_enc_out_layer].buffer
        self.dec_states_h = enc_res[self.args.hidden_layer].buffer
        self.dec_states_c = enc_res[self.args.context_layer].buffer
        self.output = enc_res[self.args.init_0_layer].buffer
        self.tgt = np.array([[START_TOKEN]])
        self.logits = []


class Renderer:
    class Status(Enum):
        ready = 0
        rendering = 1

    def __init__(self):
        with tempfile.NamedTemporaryFile() as temp_file:
            temp_file_name = temp_file.name
        self.output_file = f'{temp_file_name}.png'
        self.cur_formula = None
        self.res_img = None
        self._state = Renderer.Status.ready
        self._worker = ThreadPool(processes=1)
        self._async_result = None

    def render(self, formula):
        if self.cur_formula is None:
            self.cur_formula = formula
        elif self.cur_formula == formula:
            return self.output_file
        self.cur_formula = formula
        try:
            sympy.preview(f'$${formula}$$', viewer='file',
                          filename=self.output_file, euler=False, dvioptions=['-D', f'{DENSITY}'])
            self.res_img = cv.imread(self.output_file)
        except Exception:
            self.res_img = None
        return self.res_img, self.cur_formula

    def thread_render(self, formula):
        if self._state == Renderer.Status.ready:
            self._async_result = self._worker.apply_async(self.render, args=(formula,))
            self._state = Renderer.Status.rendering
            return None
        if self._state == Renderer.Status.rendering:
            if self._async_result.ready() and self._async_result.successful():
                self._state = Renderer.Status.ready
                return self.res_img, self.cur_formula
            elif self._async_result.ready() and not self._async_result.successful():
                self._state = Renderer.Status.ready
            return None


class Vocab:
    """Vocabulary class which helps to get
    human readable formula from sequence of integer tokens
    """

    def __init__(self, vocab_path):
        assert vocab_path.endswith(".json"), "Wrong extension of the vocab file"
        with open(vocab_path, "r") as f:
            vocab_dict = json.load(f)
            vocab_dict['id2sign'] = {int(k): v for k, v in vocab_dict['id2sign'].items()}

        self.id2sign = vocab_dict["id2sign"]

    def construct_phrase(self, indices):
        """Function to get latex formula from sequence of tokens

        Args:
            indices (list): sequence of int

        Returns:
            str: decoded formula
        """
        phrase_converted = []
        for token in indices:
            if token == END_TOKEN:
                break
            phrase_converted.append(
                self.id2sign.get(token, "?"))
        return " ".join(phrase_converted)
