#!/usr/bin/env python3
"""
 Copyright (c) 2020 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import asyncio
import logging as log
import os
import sys
import tempfile
from argparse import SUPPRESS, ArgumentParser

import cv2 as cv
import numpy as np
from utils import *

CONFIDENCE_THRESH = 0.95


class InteractiveDemo:
    def __init__(self, input_model_shape, resolution=DEFAULT_RESOLUTION, device_id=0):
        self._resolution = resolution
        self._device_id = device_id
        self._tgt_shape = input_model_shape
        self.start_point, self.end_point = self._create_input_window()
        self._prev_rendered_formula = None
        self._prev_formula_img = None
        self._latex_h = 0
        self._renderer = create_renderer()

    def __enter__(self):
        self.capture = cv.VideoCapture(self._device_id)
        self.capture.set(cv.CAP_PROP_BUFFERSIZE, 1)
        self.capture.set(3, self._resolution[0])
        self.capture.set(4, self._resolution[1])
        return self

    def get_frame(self):
        ret, frame = self.capture.read()
        return frame

    def _create_input_window(self):
        aspect_ratio = self._tgt_shape[0] / self._tgt_shape[1]
        default_width = DEFAULT_WIDTH
        height = int(default_width * aspect_ratio)
        start_point = (int(self._resolution[0] / 2 - default_width / 2), int(self._resolution[1] / 2 - height / 2))
        end_point = (int(self._resolution[0] / 2 + default_width / 2), int(self._resolution[1] / 2 + height / 2))
        return start_point, end_point

    def get_crop(self, frame):
        crop = frame[self.start_point[1]:self.end_point[1], self.start_point[0]:self.end_point[0], :]
        return crop

    def draw_rectangle(self, frame):
        frame = cv.rectangle(frame, self.start_point, self.end_point, color=RED, thickness=2)
        return frame

    def resize_window(self, action):
        height = self.end_point[1] - self.start_point[1]
        width = self.end_point[0] - self.start_point[0]
        aspect_ratio = height / width
        if action == 'increase':
            if height >= MAX_HEIGHT or width >= MAX_WIDTH:
                return
            self.start_point = (self.start_point[0]-DEFAULT_RESIZE_STEP,
                                self.start_point[1] - int(DEFAULT_RESIZE_STEP * aspect_ratio))
            self.end_point = (self.end_point[0]+DEFAULT_RESIZE_STEP,
                              self.end_point[1] + int(DEFAULT_RESIZE_STEP * aspect_ratio))
        elif action == 'decrease':
            if height <= MIN_HEIGHT or width <= MIN_WIDTH:
                return
            self.start_point = (self.start_point[0]+DEFAULT_RESIZE_STEP,
                                self.start_point[1] + int(DEFAULT_RESIZE_STEP * aspect_ratio))
            self.end_point = (self.end_point[0]-DEFAULT_RESIZE_STEP,
                              self.end_point[1] - int(DEFAULT_RESIZE_STEP * aspect_ratio))
        else:
            raise ValueError(f"wrong action: {action}")

    def put_text(self, frame, text):
        if text == '':
            return frame
        text = strip_internal_spaces(text)
        (txt_w, self._latex_h), baseLine = cv.getTextSize(text, cv.FONT_HERSHEY_SIMPLEX, 1, 3)
        start_point = (self.start_point[0],
                       self.end_point[1] - self.start_point[1] + int(self._latex_h * 1.5))
        frame = cv.putText(frame, text, org=start_point, fontFace=cv.FONT_HERSHEY_SIMPLEX,
                           fontScale=0.7, color=COLOR_BLACK, thickness=3, lineType=cv.LINE_AA)
        frame = cv.putText(frame, text, org=start_point, fontFace=cv.FONT_HERSHEY_SIMPLEX,
                           fontScale=0.7, color=COLOR_WHITE, thickness=2, lineType=cv.LINE_AA)
        comment_coords = (0, self.end_point[1] - self.start_point[1] + int(self._latex_h * 1.5))
        frame = cv.putText(frame, "Predicted:", comment_coords,
                           fontFace=cv.FONT_HERSHEY_SIMPLEX, fontScale=0.7, color=COLOR_WHITE, thickness=2, lineType=cv.LINE_AA)
        return frame

    def put_crop(self, frame, crop):
        height = self.end_point[1] - self.start_point[1]
        width = self.end_point[0] - self.start_point[0]
        crop = cv.resize(crop, (width, height))
        frame[0:height, self.start_point[0]:self.end_point[0], :] = crop
        comment_coords = (0, 20)
        frame = cv.putText(frame, "Model input:", comment_coords, fontFace=cv.FONT_HERSHEY_SIMPLEX,
                           fontScale=0.7, color=COLOR_WHITE, thickness=2, lineType=cv.LINE_AA)
        return frame

    def put_formula_img(self, frame, formula):
        if self._renderer is None or formula == '':
            return frame
        formula_img = self._render_formula_async(formula)
        if formula_img is None:
            return frame
        y_start = self.end_point[1] - self.start_point[1] + self._latex_h * 2
        img_shape = formula_img.shape
        formula_img = self._resize_if_need(formula_img)
        frame[y_start:y_start + formula_img.shape[0],
              self.start_point[0]:self.start_point[0] + formula_img.shape[1],
              :] = formula_img
        comment_coords = (0, y_start + (formula_img.shape[0] + self._latex_h) // 2)
        frame = cv.putText(frame, "Rendered:", comment_coords,
                           fontFace=cv.FONT_HERSHEY_SIMPLEX, fontScale=0.7, color=COLOR_WHITE, thickness=2, lineType=cv.LINE_AA)
        return frame

    def _resize_if_need(self, formula_img):
        if (self.end_point[0] - self.start_point[0]) < formula_img.shape[1]:
            scale_factor = (self.end_point[0] - self.start_point[0]) / formula_img.shape[1]
            formula_img = cv.resize(
                formula_img, fx=scale_factor, fy=scale_factor, dsize=None)
        return formula_img

    def _render_formula_async(self, formula):
        if formula == self._prev_rendered_formula:
            return self._prev_formula_img
        result = self._renderer.thread_render(formula)
        if result is None:
            return None
        formula_img, res_formula = result
        if res_formula != formula:
            return None
        self._prev_rendered_formula = formula
        self._prev_formula_img = formula_img
        return formula_img

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.capture.release()
        cv.destroyAllWindows()


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help',
                      default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument("-m_encoder", help="Required. Path to an .xml file with a trained encoder part of the model",
                      required=True, type=str)
    args.add_argument("-m_decoder", help="Required. Path to an .xml file with a trained decoder part of the model",
                      required=True, type=str)
    args.add_argument("--interactive", help="Optional. Enables interactive mode. In this mode images are read from the web-camera.",
                      action='store_true', default=False)
    args.add_argument("-i", "--input", help="Optional. Path to a folder with images or path to an image files",
                      required=False, type=str)
    args.add_argument("-o", "--output_file",
                      help="Optional. Path to file where to store output. If not mentioned, result will be stored"
                      "in the console.",
                      type=str)
    args.add_argument("--vocab_path", help="Required. Path to vocab file to construct meaningful phrase",
                      type=str, required=True)
    args.add_argument("--max_formula_len",
                      help="Optional. Defines maximum length of the formula (number of tokens to decode)",
                      default="128", type=int)
    args.add_argument("--conf_thresh", help="Optional. Probability threshold to trat model prediction as meaningful",
                      default=CONFIDENCE_THRESH, type=float)
    args.add_argument("-d", "--device",
                      help="Optional. Specify the target device to infer on; CPU, GPU, FPGA, HDDL or MYRIAD is "
                           "acceptable. Sample will look for a suitable plugin for device specified. Default value is CPU",
                      default="CPU", type=str)
    args.add_argument('--preprocessing_type', choices=PREPROCESSING.keys(),
                      help="Optional. Type of the preprocessing", default='crop')
    args.add_argument('-pc', '--perf_counts',
                      action='store_true', default=False)
    args.add_argument('--imgs_layer', help='Optional. Encoder input name for images. See README for details.',
                      default='imgs')
    args.add_argument('--row_enc_out_layer', help='Optional. Encoder output key for row_enc_out. See README for details.',
                      default='row_enc_out')
    args.add_argument('--hidden_layer', help='Optional. Encoder output key for hidden. See README for details.',
                      default='hidden')
    args.add_argument('--context_layer', help='Optional. Encoder output key for context. See README for details.',
                      default='context')
    args.add_argument('--init_0_layer', help='Optional. Encoder output key for init_0. See README for details.',
                      default='init_0')
    args.add_argument('--dec_st_c_layer', help='Optional. Decoder input key for dec_st_c. See README for details.',
                      default='dec_st_c')
    args.add_argument('--dec_st_h_layer', help='Optional. Decoder input key for dec_st_h. See README for details.',
                      default='dec_st_h')
    args.add_argument('--dec_st_c_t_layer', help='Optional. Decoder output key for dec_st_c_t. See README for details.',
                      default='dec_st_c_t')
    args.add_argument('--dec_st_h_t_layer', help='Optional. Decoder output key for dec_st_h_t. See README for details.',
                      default='dec_st_h_t')
    args.add_argument('--output_layer', help='Optional. Decoder output key for output. See README for details.',
                      default='output')
    args.add_argument('--output_prev_layer', help='Optional. Decoder input key for output_prev. See README for details.',
                      default='output_prev')
    args.add_argument('--logit_layer', help='Optional. Decoder output key for logit. See README for details.',
                      default='logit')
    args.add_argument('--tgt_layer', help='Optional. Decoder input key for tgt. See README for details.',
                      default='tgt')
    return parser


def main():
    log.basicConfig(format="[ %(levelname)s ] %(message)s",
                    level=log.INFO, stream=sys.stdout)

    log.info("Starting inference")
    args = build_argparser().parse_args()
    model = Model(args)
    if not args.interactive:
        renderer = create_renderer()
        for rec in tqdm(model.images_list):
            image = rec['img']
            logits, targets = model.infer_sync(image)
            prob = calculate_probability(logits)
            log.info("Confidence score is %s", prob)
            if prob >= args.conf_thresh:
                phrase = model.vocab.construct_phrase(targets)
                if args.output_file:
                    with open(args.output_file, 'a') as output_file:
                        output_file.write(rec['img_name'] + '\t' + phrase + '\n')
                else:
                    print("\n\tImage name: {}\n\tFormula: {}\n".format(rec['img_name'], phrase))
                    if renderer is not None:
                        rendered_formula, _ = renderer.render(phrase)
                        cv.imshow("Predicted formula", rendered_formula)
                        cv.waitKey(0)
    else:

        *_, height, width = model.encoder.input_info['imgs'].input_data.shape
        prev_text = ''
        with InteractiveDemo((height, width)) as demo:
            while True:
                frame = demo.get_frame()
                bin_crop = demo.get_crop(frame)
                model_input = prerocess_crop(bin_crop, (height, width), preprocess_type=args.preprocessing_type)
                frame = demo.put_crop(frame, model_input)
                model_res = model.infer_async(model_input)
                if not model_res:
                    phrase = prev_text
                else:
                    logits, targets = model_res
                    prob = calculate_probability(logits)
                    log.info("Confidence score is %s", prob)
                    if prob >= args.conf_thresh ** len(logits):
                        log.info("Prediction updated")
                        phrase = model.vocab.construct_phrase(targets)
                    else:
                        log.info("Confidence score is low, prediction is not complete")
                        phrase = ''
                frame = demo.put_text(frame, phrase)
                frame = demo.put_formula_img(frame, phrase)
                prev_text = phrase
                frame = demo.draw_rectangle(frame)
                cv.imshow('Press Q to quit.', frame)
                key = cv.waitKey(1) & 0xFF
                if key == ord('q'):
                    break
                elif key == ord('o'):
                    demo.resize_window("decrease")
                elif key == ord('p'):
                    demo.resize_window("increase")

    log.info("This demo is an API example, for any performance measurements please use the dedicated benchmark_app tool "
             "from the openVINO toolkit\n")


if __name__ == '__main__':
    sys.exit(main() or 0)
