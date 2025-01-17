import json
import time
from pathlib import Path
from typing import Union, List

import requests

from videotrans.configure import config
from videotrans.configure._base import BaseCon
from videotrans.configure._except import IPLimitExceeded
from videotrans.util import tools


class BaseTrans(BaseCon):

    def __init__(self,
                 text_list: Union[List, str] = "",
                 target_language_name="",# AI翻译渠道下是语言名称，其他渠道下是语言代码
                 inst=None,
                 source_code="",
                 uuid=None,
                 is_test=False,
                 target_code=""
                 ):
        # 目标语言，语言代码或文字名称
        super().__init__()
        self.api_url = ''
        self.target_language_name = target_language_name
        # trans_create实例
        self.inst = inst
        # 原始语言代码
        self.source_code = source_code
        self.target_code = target_code
        # 任务文件绑定id
        self.uuid = uuid
        # 用于测试
        self.is_test = is_test
        #
        self.error = ""
        self.error_code=0
        # 同时翻译字幕条数
        self.trans_thread = int(config.settings.get('trans_thread', 5))
        # 出错重试次数
        self.retry = int(config.settings.get('retries', 2))
        # 每次翻译请求完成后等待秒数
        self.wait_sec = float(config.settings.get('translation_wait', 0))
        # 当前已重试次数
        self.iter_num = 0
        # 原始需翻译的字符串或字幕list
        self.text_list = text_list
        # 翻译后的结果文本存储
        self.target_list = []
        # 如果 text_list 不是字符串则是字幕格式
        self.is_srt = False if isinstance(text_list, str) else True
        # 非AI翻译时强制设为False，是AI翻译时根据配置确定
        self.aisendsrt = True if config.settings.get('aisendsrt', False) and self.trans_thread > 1 else False
        self.refine3=True if self.aisendsrt and not self.is_test and config.settings.get('refine3',False) else False
        # 整理待翻译的文字为 List[str]
        self.split_source_text = []
        self.proxies = None
        self.model_name=""

    # 发出请求获取内容 data=[text1,text2,text] | text
    def _item_task(self, data: Union[List[str], str]) -> str:
        raise Exception('The method must be')

    def _exit(self):
        if config.exit_soft or (config.current_status != 'ing' and config.box_trans != 'ing' and not self.is_test):
            return True
        return False

    # 实际操作 # 出错时发送停止信号
    def run(self) -> Union[List, str, None]:
        # 开始对分割后的每一组进行处理
        self._signal(text="")
        if self.is_srt:
            source_text = [t['text'] for t in self.text_list] if not self.aisendsrt else self.text_list
            self.split_source_text = [source_text[i:i + self.trans_thread] for i in
                                      range(0, len(self.text_list), self.trans_thread)]
        else:
            source_text = self.text_list.strip().split("\n")
            self.split_source_text = [source_text[i:i + self.trans_thread] for i in
                                      range(0, len(source_text), self.trans_thread)]

        if self.is_srt and self.aisendsrt:
            return self.runsrt()

        for i, it in enumerate(self.split_source_text):
            # 失败后重试 self.retry 次
            while 1:
                if self._exit():
                    return
                if self.iter_num > self.retry:
                    msg = f'{"字幕翻译失败" if config.defaulelang == "zh" else " Translation Subtitles error"},{self.error}'
                    self._signal(text=msg, type="error")
                    raise Exception(msg)

                self.iter_num += 1
                if self.iter_num > 1:
                    if self.error_code==429:
                        msg='429 超出api每分钟频率限制，暂停60s后重试' if config.defaulelang=='zh' else '429 Exceeded the frequency limit of the api per minute, pause for 60s and retry'
                        self._signal(text=msg)
                        if self.inst and self.inst.status_text:
                            self.inst.status_text=msg
                        time.sleep(60)
                    else:
                        self._signal(
                            text=f"第{self.iter_num}次出错，{self.wait_sec}s后重试," if config.defaulelang == 'zh' else f'{self.iter_num} retries occurs, {self.wait_sec}s later retry')
                        time.sleep(self.wait_sec)

                try:
                    result = self._get_cache(it)
                    if not result:
                        result = tools.cleartext(self._item_task(it))
                        self._set_cache(it, result)
                    if self.inst and self.inst.precent < 75:
                        self.inst.precent += 0.01
                    # 非srt直接break
                    if not self.is_srt:
                        self.target_list.append(result)
                        self.iter_num = 0
                        self.error = ''
                        break
                    sep_res = result.split("\n")
                    raw_len = len(it)
                    sep_len = len(sep_res)

                    # 如果返回数量和原始语言数量不一致，则重新切割
                    if sep_len + 1 < raw_len:
                        sep_res = []
                        for it_n in it:
                            time.sleep(self.wait_sec)
                            t = self._get_cache(it_n)
                            if not t:
                                t = tools.cleartext(self._item_task(it_n))
                                self._set_cache(it_n, t)
                            self._signal(
                                text=t + "\n",
                                type='subtitle')
                            sep_res.append(t)

                    for x, result_item in enumerate(sep_res):
                        if x < len(it):
                            self.target_list.append(result_item.strip())
                            self._signal(
                                text=result_item + "\n",
                                type='subtitle')
                            self._signal(
                                text=config.transobj['starttrans'] + f' {i * self.trans_thread + x + 1} ')
                    if len(sep_res) < len(it):
                        tmp = ["" for x in range(len(it) - len(sep_res))]
                        self.target_list += tmp
                except requests.exceptions.ProxyError as e:
                    proxy=None if not self.proxies else f'{list(self.proxies.values())[0]}'
                    raise Exception(f'代理错误请检查:{proxy=} {e}')
                except (requests.ConnectionError, requests.exceptions.RetryError, requests.Timeout) as e:
                    msg=''
                    if self.api_url:
                        msg = f'无法连接当前API:{self.api_url} ' if config.defaulelang == 'zh' else f'Check API:{self.api_url} '
                    raise IPLimitExceeded(msg=msg+str(e), name=self.__class__.__name__)
                except Exception as e:
                    self.error = f'{e}'
                    config.logger.exception(e, exc_info=True)
                else:
                    # 成功 未出错
                    self.error = ''
                    self.error_code=0
                    self.iter_num = 0
                    if self.inst and self.inst.status_text:
                        self.inst.status_text='字幕翻译中' if config.defaulelang=='zh' else 'Translation of subtitles'
                finally:
                    time.sleep(self.wait_sec)
                if self.iter_num == 0:
                    # 未出错跳过while
                    break
        # 恢复原代理设置
        if self.shound_del:
            self._set_proxy(type='del')
        # text_list是字符串
        if not self.is_srt:
            return "\n".join(self.target_list)

        max_i = len(self.target_list)
        # 出错次数大于原一半
        if max_i < len(self.text_list) / 2:
            msg = f'{config.transobj["fanyicuowu2"]}:{self.error}'
            self._signal(text=msg, type="error")
            raise Exception(f'[{self.__class__.__name__}]:{msg}')

        for i, it in enumerate(self.text_list):
            if i < max_i:
                self.text_list[i]['text'] = self.target_list[i]
            else:
                self.text_list[i]['text'] = ""
        return self.text_list

    # 发送完整字幕格式内容进行翻译
    def runsrt(self):
        result_srt_str_list = []
        for i, it in enumerate(self.split_source_text):
            # 失败后重试 self.retry 次
            while 1:
                if self._exit():
                    return
                if self.iter_num > self.retry:
                    msg = f'{"字幕翻译阶段失败" if config.defaulelang == "zh" else " Translate subtitles error "},{self.error}'
                    self._signal(text=msg, type="error")
                    raise Exception(msg)

                self.iter_num += 1
                if self.iter_num > 1:
                    if self.error_code==429:
                        msg='429 超出api每分钟频率限制，暂停60s后重试' if config.defaulelang=='zh' else '429 Exceeded the frequency limit of the api per minute, pause for 60s and retry'
                        self._signal(text=msg)
                        if self.inst and self.inst.status_text:
                            self.inst.status_text=msg
                        time.sleep(60)
                    else:
                        self._signal(
                            text=f"第{self.iter_num}次出错，{self.wait_sec} s后重试," if config.defaulelang == 'zh' else f'{self.iter_num} retries occurs, {self.wait_sec}s later retry')
                        time.sleep(self.wait_sec)

                try:
                    srt_str = "\n\n".join(
                        [f"{srtinfo['line']}\n{srtinfo['time']}\n{srtinfo['text'].strip()}" for srtinfo in it])
                    result = self._get_cache(srt_str)
                    if not result:
                        result = tools.cleartext(self._item_task(srt_str))
                        if not result.strip():
                            raise Exception('无返回翻译结果' if config.defaulelang == 'zh' else 'Translate result is empty')
                        self._set_cache(it, result)

                    if self.inst and self.inst.precent < 75:
                        self.inst.precent += 0.1

                    self._signal(text=result, type='subtitle')
                    result_srt_str_list.append(result)
                except requests.exceptions.ProxyError as e:
                    proxy=None if not self.proxies else f'{list(self.proxies.values())[0]}'
                    raise Exception(f'代理错误请检查:{proxy=} {e}')
                except (requests.ConnectionError, requests.Timeout, requests.exceptions.RetryError) as e:
                    msg=''
                    if self.api_url:
                        msg = f'无法连接当前API:{self.api_url} ' if config.defaulelang == 'zh' else f'Check API:{self.api_url} '
                    raise IPLimitExceeded(msg=msg+str(e), name=self.__class__.__name__)
                except Exception as e:
                    self.error = f'{e}'
                    config.logger.exception(e, exc_info=True)
                else:
                    # 成功 未出错
                    self.error = ''
                    self.error_code=0
                    self.iter_num = 0
                    if self.inst and self.inst.status_text:
                        self.inst.status_text='字幕翻译中' if config.defaulelang=='zh' else 'Translation of subtitles'
                finally:
                    time.sleep(self.wait_sec)
                if self.iter_num == 0:
                    # 未出错跳过while
                    break

        # 恢复原代理设置
        if self.shound_del:
            self._set_proxy(type='del')
        return tools.get_subtitle_from_srt("\n\n".join(result_srt_str_list), is_file=False)


    def _replace_prompt(self):
        if self.is_srt and self.aisendsrt and self.source_code and self.target_code and self.source_code in config.explames and self.target_code in config.explames:
            replace_str='**示例:**\n' if config.defaulelang=='zh' else '**Example:**\n'
            replace_str+=config.explames[self.source_code]
            replace_str+='\n\n译文:\n' if config.defaulelang=='zh' else '**Translation:**\n'
            replace_str+=config.explames[self.target_code]
            self.prompt=self.prompt.replace('<source>[TEXT]',replace_str+'\n\n<source>[TEXT]')
        return self.prompt

    def _refine3_prompt(self):
        zh_prompt="""
        # 三步反思法翻译srt字幕

您是一位技术娴熟的翻译人员，专门负责将 SRT 格式的字幕从其他语言精准翻译成{lang}语言的 SRT 格式字幕。请仔细按以下要求完成翻译：

## 输入
提供的内容为合法的 SRT 字幕格式。请确保在翻译中严格保持 SRT 格式正确，不添加或省略任何内容。

## 翻译流程
请按照以下三步流程执行翻译任务：

1. **初步翻译**：
   - 将字幕内容翻译成{lang}语言，忠实保留原意，并确保格式完全保持 SRT 标准。
   - 不增加或删减任何信息，不添加解释或说明。

2. **翻译改进建议**：
   - 仔细比对原文和译文，提出具体改进建议，提升翻译准确性与流畅性。建议内容包括：
     - **准确性**：纠正可能的误译、遗漏或添加多余信息。
     - **流畅性**：确保符合{lang}的语法、拼写和标点规则，避免不必要的重复。
     - **简洁性**：在保持原意的前提下，优化译文的简洁度，避免冗长。
     - **格式正确性**：确保翻译后 SRT 字幕格式有效，字幕条数与原文一致。

3. **润色与完善**：
   - 根据初步翻译和改进建议，进一步优化和润色译文，确保翻译忠实、简洁短小、口语化。
   - 不要添加解释或附加说明，确保最终字幕符合 SRT 格式要求，且条数与原文一致。

## 输出格式

请使用以下 XML 标签结构，分别输出每个步骤的结果：

```xml
<step1_initial_translation>
[插入初步翻译结果]
</step1_initial_translation>

<step2_reflection>
[插入针对改进的具体建议，每条建议应对应一个翻译改进方面]
</step2_reflection>

<step3_refined_translation>
[插入润色后的最终翻译结果]
</step3_refined_translation>
```

## 注意事项
- 始终确保最终翻译保留原文含义，并严格符合 SRT 格式。
- 输出的字幕数量须与原始字幕一致。

以下<INPUT>标签内为需翻译的 SRT 字幕内容：

<INPUT></INPUT>
        
        """
        en_prompt="""        
# Three-step reflection method for translating SRT subtitles

You are a skilled translator specializing in translating SRT subtitles from other languages into {lang} SRT subtitles. Please carefully complete the translation according to the following requirements:

## Input

The provided content is in a legal SRT subtitle format. Please ensure that the SRT format is strictly maintained throughout the translation process, with no content added or omitted. 

## Translation Process

Please follow the following three-step process to perform the translation task:

1. **Preliminary Translation**:
   - Translate the subtitle content into {lang}, faithfully retaining the original meaning and ensuring the format fully adheres to the SRT standard.
   - Do not add or delete any information, nor include explanations or instructions.

2. **Translation Improvement Suggestions**:
   - Carefully compare the original text and the translated text, providing specific improvement suggestions to enhance the accuracy and fluency of the translation. These suggestions should focus on:
     - **Accuracy**: Correct potential mistranslations, omissions, or the addition of redundant information.
     - **Fluency**: Ensure the grammar, spelling, and punctuation rules of {lang} are met, avoiding unnecessary repetition.
     - **Conciseness**: Optimize the conciseness of the translation, avoiding verbosity while preserving the original meaning.
     - **Format Correctness**: Ensure the SRT subtitle format remains valid after translation, with the number of subtitles consistent with the original text.

3. **Polishing and Perfection**:
   - Based on the preliminary translation and improvement suggestions, further optimize and polish the translation to ensure it is faithful, concise, and fluent.
   - Do not add explanations or additional instructions. Ensure the final subtitles meet the SRT format requirements and that the number of subtitles is consistent with the original text.

## Output Format

Please use the following XML tag structure to output the results of each step separately:

```xml
<step1_initial_translation>
[Insert initial translation results]
</step1_initial_translation>

<step2_reflection>
[Insert specific suggestions for improvement, each suggestion corresponding to one aspect of translation improvement]
</step2_reflection>

<step3_refined_translation>
[Insert final translation after polishing]
</step3_refined_translation>
```

## Notes

- Always ensure the final translation retains the original meaning and strictly conforms to the SRT format.
- The number of subtitles output must be the same as the original subtitles.

The following `<INPUT>` tags contain the SRT subtitles to be translated:

<INPUT></INPUT>

        """
        return zh_prompt if config.defaulelang=='zh' else en_prompt

    def _set_cache(self, it, res_str):
        if not res_str.strip():
            return
        key_cache = self._get_key(it)

        file_cache = config.TEMP_DIR + f'/translate_cache/{key_cache}.txt'
        if not Path(config.TEMP_DIR + f'/translate_cache').is_dir():
            Path(config.TEMP_DIR + f'/translate_cache').mkdir(parents=True, exist_ok=True)
        Path(file_cache).write_text(res_str, encoding='utf-8')

    def _get_cache(self, it):
        if self.is_test:
            return None
        key_cache = self._get_key(it)
        file_cache = config.TEMP_DIR + f'/translate_cache/{key_cache}.txt'
        if Path(file_cache).exists():
            return Path(file_cache).read_text(encoding='utf-8')
        return None

    def _get_key(self, it):
        Path(config.TEMP_DIR + '/translate_cache').mkdir(parents=True, exist_ok=True)
        return tools.get_md5(
            f'{self.__class__.__name__}-{self.model_name}-{self.source_code}-{self.target_code}-{it if isinstance(it, str) else json.dumps(it)}')
