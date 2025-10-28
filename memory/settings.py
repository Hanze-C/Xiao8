import json
import asyncio
from langchain_openai import ChatOpenAI
from openai import RateLimitError
from config import get_core_config, SETTING_PROPOSER_MODEL, SETTING_VERIFIER_MODEL, get_character_data
from config.prompts_sys import settings_extractor_prompt, settings_verifier_prompt


class ImportantSettingsManager:
    def __init__(self):
        self.settings = {}
        self.settings_file = None
    
    def _get_proposer(self):
        """动态获取Proposer LLM实例以支持配置热重载"""
        core_config = get_core_config()
        return ChatOpenAI(model=SETTING_PROPOSER_MODEL, base_url=core_config['OPENROUTER_URL'], api_key=core_config['OPENROUTER_API_KEY'], temperature=0.5)
    
    def _get_verifier(self):
        """动态获取Verifier LLM实例以支持配置热重载"""
        core_config = get_core_config()
        return ChatOpenAI(model=SETTING_VERIFIER_MODEL, base_url=core_config['OPENROUTER_URL'], api_key=core_config['OPENROUTER_API_KEY'], temperature=0.5)

    def load_settings(self):
        # It is important to update the settings with the latest character on-disk files
        _, _, master_basic_config, lanlan_basic_config, name_mapping, _, _, _, setting_store, _ = get_character_data()
        self.settings_file = setting_store
        self.master_basic_config = master_basic_config
        self.lanlan_basic_config = lanlan_basic_config
        self.name_mapping = name_mapping

        for i in self.settings_file:
            try:
                self.lanlan_basic_config[i].pop('system_prompt', None)
                self.lanlan_basic_config[i].pop('live2d', None)
                self.lanlan_basic_config[i].pop('voice_id', None)
                with open(self.settings_file[i], 'r', encoding='utf-8') as f:
                    self.settings[i] = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self.settings[i] = {i: {}, self.name_mapping['human']: {}}

    def save_settings(self, lanlan_name):
        with open(self.settings_file[lanlan_name], 'w', encoding='utf-8') as f:
            json.dump(self.settings[lanlan_name], f, indent=2, ensure_ascii=False)

    async def detect_and_resolve_contradictions(self, old_settings, new_settings, lanlan_name):
        # 使用LLM检测矛盾并解决它们
        prompt = settings_verifier_prompt % (json.dumps(old_settings, ensure_ascii=False), json.dumps(new_settings, ensure_ascii=False))
        prompt = prompt.replace("{LANLAN_NAME}", lanlan_name)

        retries = 0
        max_retries = 3
        while retries < max_retries:
            try:
                verifier = self._get_verifier()
                response = await verifier.ainvoke(prompt)
                result = response.content
                if result.startswith("```"):
                    result = result .replace("```json", "").replace("```", "").strip()
            except RateLimitError as e:
                retries += 1
                if retries >= max_retries:
                    print(f"❌ Setting resolver query失败，已达到最大重试次数: {e}")
                    return old_settings
                # 指数退避: 1, 2, 4 秒
                wait_time = 2 ** (retries - 1)
                print(f'⚠️ 遇到429错误，等待 {wait_time} 秒后重试 (第 {retries}/{max_retries} 次)')
                await asyncio.sleep(wait_time)
                continue
            except Exception as e:
                print(f"❌ Setting resolver query出错: {e}")
                retries += 1
                continue
            try:
                merged_settings = json.loads(result)
                return merged_settings
            except json.JSONDecodeError:
                # 如果解析失败，返回新设定
                retries += 1
                print(f"❌ Setting resolver返回值解析失败。返回值：{response.content}")
        return old_settings

    async def extract_and_update_settings(self, messages, lanlan_name):
        name_mapping = self.name_mapping.copy()
        name_mapping['ai'] = lanlan_name
        lines = []
        for msg in messages:
            try:
                parts = []
                for i in msg.content:
                    if isinstance(i, dict):
                        parts.append(i.get("text", f"|{i.get('type','')}|"))
                    else:
                        parts.append(str(i))
                joined = "\n".join(parts)
            except Exception:
                joined = str(getattr(msg, 'content', ''))
            lines.append(f"{name_mapping[msg.type]} | {joined}")
        prompt = settings_extractor_prompt % ("\n".join(lines))
        prompt = prompt.replace('{LANLAN_NAME}', lanlan_name)
        retries = 0
        max_retries = 3
        new_settings = ""
        while retries < max_retries:
            try:
                proposer = self._get_proposer()
                response = await proposer.ainvoke(prompt)
            except RateLimitError as e:
                retries += 1
                if retries >= max_retries:
                    print(f"❌ Setting LLM query失败，已达到最大重试次数: {e}")
                    return
                # 指数退避: 1, 2, 4 秒
                wait_time = 2 ** (retries - 1)
                print(f'⚠️ 遇到429错误，等待 {wait_time} 秒后重试 (第 {retries}/{max_retries} 次)')
                await asyncio.sleep(wait_time)
                continue
            except Exception as e:
                print(f"❌ Setting LLM query出错: {e}")
                retries += 1
                continue
            try:
                result = response.content
                if result.startswith("```"):
                    result = result .replace("```json", "").replace("```", "").strip()
                new_settings = json.loads(result)
            except json.JSONDecodeError:
                print(f"❌ Setting LLM返回的设定JSON解析失败。返回值：{response.content}")
                retries += 1
            break

        # 检测并解决矛盾
        if len(new_settings)>0:
            self.load_settings()
            self.settings[lanlan_name] = await self.detect_and_resolve_contradictions(self.settings[lanlan_name], new_settings, lanlan_name)
            self.save_settings(lanlan_name)

    def get_settings(self, lanlan_name):
        self.load_settings()
        self.settings[lanlan_name][lanlan_name].update(self.lanlan_basic_config[lanlan_name])
        self.settings[lanlan_name][self.name_mapping['human']].update(self.master_basic_config)
        return self.settings[lanlan_name]