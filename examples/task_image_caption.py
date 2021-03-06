#! -*- coding: utf-8 -*-
# bert做image caption任务，coco数据集
# 通过Conditional Layer Normalization融入条件信息
# 请参考：https://kexue.fm/archives/7124

from __future__ import print_function
import glob
import numpy as np
from tqdm import tqdm
import os, json, re
from bert4keras.backend import keras, K
from bert4keras.bert import build_bert_model
from bert4keras.tokenizer import Tokenizer, load_vocab
from bert4keras.optimizers import Adam
from bert4keras.snippets import sequence_padding, is_string
from bert4keras.snippets import DataGenerator, AutoRegressiveDecoder
import cv2


# 模型配置
maxlen = 64
batch_size = 32
steps_per_epoch = 1000
epochs = 10000

# bert配置
config_path = '/root/kg/bert/uncased_L-12_H-768_A-12/bert_config.json'
checkpoint_path = '/root/kg/bert/uncased_L-12_H-768_A-12/bert_model.ckpt'
dict_path = '/root/kg/bert/uncased_L-12_H-768_A-12/vocab.txt'

# 加载并精简词表，建立分词器
token_dict, keep_tokens = load_vocab(
    dict_path=dict_path,
    simplified=True,
    startwith=['[PAD]', '[UNK]', '[CLS]', '[SEP]'],
)
tokenizer = Tokenizer(token_dict, do_lower_case=True)


def read_caption(f):
    """读取并整理COCO的Caption数据
    """
    data = json.load(open(f))
    images = {}
    for img in data['images']:
        images[img['id']] = {
            'image_id': img['file_name'],
            'caption': [],
            'url': img['coco_url']
        }
    for caption in data['annotations']:
        images[caption['image_id']]['caption'].append(caption['caption'])
    return list(images.values())


def read_image(f):
    """单图读取函数（对非方形的图片进行白色填充，使其变为方形）
    """
    img = cv2.imread(f)
    height, width = img.shape[:2]
    if height > width:
        height, width = img_size, width * img_size // height
        img = cv2.resize(img, (width, height))
        delta = (height - width) // 2
        img = cv2.copyMakeBorder(img,
                                 top=0,
                                 bottom=0,
                                 left=delta,
                                 right=height - width - delta,
                                 borderType=cv2.BORDER_CONSTANT,
                                 value=[255, 255, 255])
    else:
        height, width = height * img_size // width, img_size
        img = cv2.resize(img, (width, height))
        delta = (width - height) // 2
        img = cv2.copyMakeBorder(img,
                                 top=delta,
                                 bottom=width - height - delta,
                                 left=0,
                                 right=0,
                                 borderType=cv2.BORDER_CONSTANT,
                                 value=[255, 255, 255])
    img = img.astype('float32')
    return img[..., ::-1]  # cv2的读取模式为BGR，但keras的模型要求为RGB


class data_generator(DataGenerator):
    """数据生成器
    """
    def __iter__(self, random=False):
        idxs = list(range(len(self.data)))
        if random:
            np.random.shuffle(idxs)
        batch_images, batch_token_ids, batch_segment_ids = [], [], []
        for i in idxs:
            D = self.data[i]
            img = '/root/caption/coco/train2014/%s' % D['image_id']
            caption = np.random.choice(D['caption'])
            token_ids, segment_ids = tokenizer.encode(caption,
                                                      max_length=maxlen)
            batch_images.append(read_image(img))
            batch_token_ids.append(token_ids)
            batch_segment_ids.append(segment_ids)
            if len(batch_token_ids) == self.batch_size or i == idxs[-1]:
                batch_images = np.array(batch_images)
                batch_images = preprocess_input(batch_images)
                batch_token_ids = sequence_padding(batch_token_ids)
                batch_segment_ids = sequence_padding(batch_segment_ids)
                yield [batch_token_ids, batch_segment_ids, batch_images], None
                batch_images, batch_token_ids, batch_segment_ids = [], [], []


# 加载数据
train_data = read_caption('/root/caption/coco/annotations/captions_train2014.json')
valid_data = read_caption('/root/caption/coco/annotations/captions_val2014.json')


# 图像模型
MobileNetV2 = keras.applications.mobilenet_v2.MobileNetV2
preprocess_input = keras.applications.mobilenet_v2.preprocess_input
image_model = MobileNetV2(include_top=False, pooling='avg')
img_size = 299

# Bert模型
model = build_bert_model(
    config_path,
    checkpoint_path,
    application='lm',
    keep_tokens=keep_tokens,  # 只保留keep_tokens中的字，精简原字表
    layer_norm_cond=image_model.output,
    layer_norm_cond_hidden_size=128,
    layer_norm_cond_hidden_act='swish',
    additional_input_layers=image_model.input,
)

model.summary()

# 交叉熵作为loss，并mask掉输入部分的预测
y_in = model.input[0][:, 1:]  # 目标tokens
y_mask = model.get_layer('Sequence-Mask').output_mask[:, 1:]  # 目标mask
y = model.output[:, :-1]  # 预测tokens，预测与目标错开一位
cross_entropy = K.sparse_categorical_crossentropy(y_in, y)
cross_entropy = K.sum(cross_entropy * y_mask) / K.sum(y_mask)

model.add_loss(cross_entropy)
model.compile(optimizer=Adam(1e-5))


class AutoCaption(AutoRegressiveDecoder):
    """img2seq解码器
    """
    def predict(self, inputs, output_ids, step, rtype='logits'):
        image = inputs[0]
        token_ids = output_ids
        segment_ids = np.zeros_like(token_ids)
        probas = model.predict([token_ids, segment_ids, image])[:, -1]
        if rtype == 'probas':
            return probas
        else:
            return np.log(probas)

    def generate(self, image, topk=1):
        if is_string(image):
            image = read_image(image)
        image = preprocess_input(image)
        output_ids = self.beam_search([image], topk)  # 基于beam search
        return tokenizer.decode(output_ids)


autocaption = AutoCaption(start_id=tokenizer._token_cls_id,
                          end_id=tokenizer._token_sep_id,
                          maxlen=maxlen)


def just_show():
    samples = [valid_data[i] for i in np.random.choice(len(valid_data), 2)]
    for D in samples:
        img = '/root/caption/coco/val2014/%s' % D['image_id']
        print(u'image_id:', D['image_id'])
        print(u'url:', D['url'])
        print(u'predict:', autocaption.generate(img))
        print(u'references:', D['caption'])
        print()


class Evaluate(keras.callbacks.Callback):
    def __init__(self):
        self.lowest = 1e10

    def on_epoch_end(self, epoch, logs=None):
        # 保存最优
        if logs['loss'] <= self.lowest:
            self.lowest = logs['loss']
            model.save_weights('./best_model.weights')
        # 演示效果
        just_show()


if __name__ == '__main__':

    evaluator = Evaluate()
    train_generator = data_generator(train_data, batch_size)

    model.fit_generator(train_generator.forfit(),
                        steps_per_epoch=steps_per_epoch,
                        epochs=epochs,
                        callbacks=[evaluator])

else:

    model.load_weights('./best_model.weights')


"""
image_id: COCO_val2014_000000524611.jpg
url: http://images.cocodataset.org/val2014/COCO_val2014_000000524611.jpg
predict: a train that is sitting on the tracks.
references: [u'A train carrying chemical tanks traveling past a water tower.', u'Dual train tracks with a train on one of them and a water tower in the background.', u'a train some trees and a water tower ', u'Train on tracks with water tower for Davis Junction in the rear.', u'A train on a train track going through a bunch of trees.']

image_id: COCO_val2014_000000202923.jpg
url: http://images.cocodataset.org/val2014/COCO_val2014_000000202923.jpg
predict: a baseball game in progress with the batter up to plate.
references: [u'Batter, catcher, and umpire anticipating the next pitch.', u'A baseball player holding a baseball bat in the game.', u'A baseball player stands ready at the plate.', u'Baseball players on the field ready for the pitch.', u'A view from behind a mesh fence of a baseball game.']
"""
