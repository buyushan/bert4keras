#! -*- coding: utf-8 -*-
# bert做Seq2Seq任务，采用UNILM方案
# 介绍链接：https://kexue.fm/archives/6933
# 数据集：https://github.com/CLUEbenchmark/CLGE
# 补充了评测指标bleu、rouge-1、rouge-2、rouge-l

from __future__ import print_function
import glob, os, json
import numpy as np
from tqdm import tqdm
from bert4keras.backend import keras, K
from bert4keras.bert import build_bert_model
from bert4keras.tokenizer import Tokenizer, load_vocab
from bert4keras.optimizers import Adam
from bert4keras.snippets import sequence_padding, open
from bert4keras.snippets import DataGenerator, AutoRegressiveDecoder
from rouge import Rouge  # pip install rouge
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction


# 基本参数
maxlen = 256
batch_size = 16
epochs = 20

# bert配置
config_path = '/root/kg/bert/chinese_wwm_L-12_H-768_A-12/bert_config.json'
checkpoint_path = '/root/kg/bert/chinese_wwm_L-12_H-768_A-12/bert_model.ckpt'
dict_path = '/root/kg/bert/chinese_wwm_L-12_H-768_A-12/vocab.txt'


def load_data(filename):
    D = []
    with open(filename, encoding='utf-8') as f:
        for l in f:
            title, content = l.strip().split('\t')
            D.append((title, content))
    return D


# 加载数据集
train_data = load_data('/root/csl/train.tsv')
valid_data = load_data('/root/csl/val.tsv')
test_data = load_data('/root/csl/test.tsv')

# 加载并精简词表，建立分词器
token_dict, keep_tokens = load_vocab(
    dict_path=dict_path,
    simplified=True,
    startwith=['[PAD]', '[UNK]', '[CLS]', '[SEP]'],
)
tokenizer = Tokenizer(token_dict, do_lower_case=True)


class data_generator(DataGenerator):
    """数据生成器
    """
    def __iter__(self, random=False):
        idxs = list(range(len(self.data)))
        if random:
            np.random.shuffle(idxs)
        batch_token_ids, batch_segment_ids = [], []
        for i in idxs:
            title, content = self.data[i]
            token_ids, segment_ids = tokenizer.encode(content,
                                                      title,
                                                      max_length=maxlen)
            batch_token_ids.append(token_ids)
            batch_segment_ids.append(segment_ids)
            if len(batch_token_ids) == self.batch_size or i == idxs[-1]:
                batch_token_ids = sequence_padding(batch_token_ids)
                batch_segment_ids = sequence_padding(batch_segment_ids)
                yield [batch_token_ids, batch_segment_ids], None
                batch_token_ids, batch_segment_ids = [], []


model = build_bert_model(
    config_path,
    checkpoint_path,
    application='seq2seq',
    keep_tokens=keep_tokens,  # 只保留keep_tokens中的字，精简原字表
)

model.summary()

# 交叉熵作为loss，并mask掉输入部分的预测
y_in = model.input[0][:, 1:]  # 目标tokens
y_mask = model.input[1][:, 1:]
y = model.output[:, :-1]  # 预测tokens，预测与目标错开一位
cross_entropy = K.sparse_categorical_crossentropy(y_in, y)
cross_entropy = K.sum(cross_entropy * y_mask) / K.sum(y_mask)

model.add_loss(cross_entropy)
model.compile(optimizer=Adam(1e-5))


class AutoTitle(AutoRegressiveDecoder):
    """seq2seq解码器
    """
    def predict(self, inputs, output_ids, step, rtype='logits'):
        token_ids, segment_ids = inputs
        token_ids = np.concatenate([token_ids, output_ids], 1)
        segment_ids = np.concatenate([segment_ids, np.ones_like(output_ids)], 1)
        probas = model.predict([token_ids, segment_ids])[:, -1]
        if rtype == 'probas':
            return probas
        else:
            return np.log(probas)

    def generate(self, text, topk=1):
        max_c_len = maxlen - self.maxlen
        token_ids, segment_ids = tokenizer.encode(text, max_length=max_c_len)
        output_ids = self.beam_search([token_ids, segment_ids], topk)  # 基于beam search
        return tokenizer.decode(output_ids)


autotitle = AutoTitle(start_id=None,
                      end_id=tokenizer._token_sep_id,
                      maxlen=32)


class Evaluate(keras.callbacks.Callback):
    def __init__(self):
        self.rouge = Rouge()
        self.smooth = SmoothingFunction().method1

    def on_epoch_end(self, epoch, logs=None):
        model.save_weights('./best_model.weights')  # 保存模型
        print('valid_data:', self.evaluate(valid_data))  # 评测模型

    def evaluate(self, data, topk=1):
        total = 0
        rouge_1, rouge_2, rouge_l, bleu = 0, 0, 0, 0
        for title, content in tqdm(data):
            total += 1
            title = ' '.join(title)
            pred_title = ' '.join(autotitle.generate(content, topk))
            scores = self.rouge.get_scores(hyps=pred_title, refs=title)
            rouge_1 += scores[0]['rouge-1']['f']
            rouge_2 += scores[0]['rouge-2']['f']
            rouge_l += scores[0]['rouge-l']['f']
            bleu += sentence_bleu(references=[title.split(' ')],
                                  hypothesis=pred_title.split(' '),
                                  smoothing_function=self.smooth)
        rouge_1 /= total
        rouge_2 /= total
        rouge_l /= total
        bleu /= total
        return {
            'rouge-1': rouge_1,
            'rouge-2': rouge_2,
            'rouge-l': rouge_l,
            'bleu': bleu,
        }


if __name__ == '__main__':

    evaluator = Evaluate()
    train_generator = data_generator(train_data, batch_size)

    model.fit_generator(train_generator.forfit(),
                        steps_per_epoch=len(train_generator),
                        epochs=epochs,
                        callbacks=[evaluator])

else:

    model.load_weights('./best_model.weights')
