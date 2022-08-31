import asyncio
import random
import time
from typing import Tuple, Union, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, ChatMember
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackContext
from telegram.helpers import escape_markdown

from core.quiz import QuizService
from logger import Log
from utils.random import MT19937_Random
from utils.service.inject import inject

FullChatPermissions = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
)


class GroupJoiningVerification:
    """群验证模块"""

    @inject
    def __init__(self, quiz_service: QuizService = None):
        self.quiz_service = quiz_service
        self.time_out = 120
        self.kick_time = 120
        self.random = MT19937_Random()
        self.lock = asyncio.Lock()
        self.chat_administrators_cache: Dict[Union[str, int], Tuple[float, list[ChatMember]]] = {}
        self.is_refresh_quiz = False

    async def refresh_quiz(self):
        async with self.lock:
            if not self.is_refresh_quiz:
                await self.quiz_service.refresh_quiz()
                self.is_refresh_quiz = True

    async def get_chat_administrators(self, context: CallbackContext, chat_id: Union[str, int]) -> list[ChatMember]:
        async with self.lock:
            cache_data = self.chat_administrators_cache.get(f"{chat_id}")
            if cache_data is not None:
                cache_time, chat_administrators = cache_data
                if time.time() >= cache_time + 360:
                    return chat_administrators
            chat_administrators = await context.bot.get_chat_administrators(chat_id)
            self.chat_administrators_cache[f"{chat_id}"] = (time.time(), chat_administrators)
            return chat_administrators

    @staticmethod
    def is_admin(chat_administrators: list[ChatMember], user_id: int) -> bool:
        return any(admin.user.id == user_id for admin in chat_administrators)

    async def kick_member_job(self, context: CallbackContext):
        job = context.job
        Log.info(f"踢出用户 user_id[{job.user_id}] 在 chat_id[{job.chat_id}]")
        try:
            await context.bot.ban_chat_member(chat_id=job.chat_id, user_id=job.user_id,
                                              until_date=int(time.time()) + self.kick_time)
        except BadRequest as error:
            Log.error(f"Auth模块在 chat_id[{job.chat_id}] user_id[{job.user_id}] 执行kick失败", error)

    @staticmethod
    async def clean_message_job(context: CallbackContext):
        job = context.job
        Log.debug(f"删除消息 chat_id[{job.chat_id}] 的 message_id[{job.data}]")
        try:
            await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)
        except BadRequest as error:
            if "not found" in str(error):
                Log.warning(f"Auth模块删除消息 chat_id[{job.chat_id}] message_id[{job.data}]失败 消息不存在")
            elif "Message can't be deleted" in str(error):
                Log.warning(
                    f"Auth模块删除消息 chat_id[{job.chat_id}] message_id[{job.data}]失败 消息无法删除 可能是没有授权")
            else:
                Log.error(f"Auth模块删除消息 chat_id[{job.chat_id}] message_id[{job.data}]失败", error)

    @staticmethod
    async def restore_member(context: CallbackContext, chat_id: int, user_id: int):
        Log.debug(f"重置用户权限 user_id[{user_id}] 在 chat_id[{chat_id}]")
        try:
            await context.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=FullChatPermissions)
        except BadRequest as error:
            Log.error(f"Auth模块在 chat_id[{chat_id}] user_id[{user_id}] 执行restore失败", error)

    async def admin(self, update: Update, context: CallbackContext) -> None:

        async def admin_callback(callback_query_data: str) -> Tuple[str, int]:
            _data = callback_query_data.split("|")
            _result = _data[1]
            _user_id = int(_data[2])
            Log.debug(f"admin_callback函数返回 result[{_result}] user_id[{_user_id}]")
            return _result, _user_id

        callback_query = update.callback_query
        user = callback_query.from_user
        message = callback_query.message
        chat = message.chat
        Log.info(f"用户 {user.full_name}[{user.id}] 在群 {chat.title}[{chat.id}] 点击Auth管理员命令")
        chat_administrators = await self.get_chat_administrators(context, chat_id=chat.id)
        if not self.is_admin(chat_administrators, user.id):
            Log.debug(f"用户 {user.full_name}[{user.id}] 在群 {chat.title}[{chat.id}] 非群管理")
            await callback_query.answer(text="你不是管理！\n"
                                             "再乱点我叫西风骑士团、千岩军和天领奉行了！", show_alert=True)
            return
        result, user_id = await admin_callback(callback_query.data)
        try:
            member_info = await context.bot.get_chat_member(chat.id, user_id)
        except BadRequest as error:
            Log.warning(f"获取用户 {user_id} 在群 {chat.title}[{chat.id}] 信息失败 \n", error)
            user_info = f"{user_id}"
        else:
            user_info = member_info.user.mention_markdown_v2()

        if result == "pass":
            await callback_query.answer(text="放行", show_alert=False)
            await self.restore_member(context, chat.id, user_id)
            if schedule := context.job_queue.scheduler.get_job(f"{chat.id}|{user_id}|auth_clean_join_message"):
                schedule.remove()
            await message.edit_text(f"{user_info} 被 {user.mention_markdown_v2()} 放行",
                                    parse_mode=ParseMode.MARKDOWN_V2)
            Log.info(f"用户 user_id[{user_id}] 在群 {chat.title}[{chat.id}] 被管理放行")
        elif result == "kick":
            await callback_query.answer(text="驱离", show_alert=False)
            await context.bot.ban_chat_member(chat.id, user_id)
            await message.edit_text(f"{user_info} 被 {user.mention_markdown_v2()} 驱离",
                                    parse_mode=ParseMode.MARKDOWN_V2)
            Log.info(f"用户 user_id[{user_id}] 在群 {chat.title}[{chat.id}] 被管理踢出")
        elif result == "unban":
            await callback_query.answer(text="解除驱离", show_alert=False)
            await self.restore_member(context, chat.id, user_id)
            if schedule := context.job_queue.scheduler.get_job(f"{chat.id}|{user_id}|auth_clean_join_message"):
                schedule.remove()
            await message.edit_text(f"{user_info} 被 {user.mention_markdown_v2()} 解除驱离",
                                    parse_mode=ParseMode.MARKDOWN_V2)
            Log.info(f"用户 user_id[{user_id}] 在群 {chat.title}[{chat.id}] 被管理解除封禁")
        else:
            Log.warning(f"auth 模块 admin 函数 发现未知命令 result[{result}]")
            await context.bot.send_message(chat.id, "派蒙这边收到了错误的消息！请检查详细日记！")
        if schedule := context.job_queue.scheduler.get_job(f"{chat.id}|{user_id}|auth_kick"):
            schedule.remove()

    async def query(self, update: Update, context: CallbackContext) -> None:

        async def query_callback(callback_query_data: str) -> Tuple[int, bool, str, str]:
            _data = callback_query_data.split("|")
            _user_id = int(_data[1])
            _question_id = int(_data[2])
            _answer_id = int(_data[3])
            _answer = await self.quiz_service.get_answer(_answer_id)
            _question = await self.quiz_service.get_question(_question_id)
            _result = _answer.is_correct
            _answer_encode = _answer.text
            _question_encode = _question.text
            Log.debug(f"query_callback函数返回 user_id[{_user_id}] result[{_result}] \n"
                      f"question_encode[{_question_encode}] answer_encode[{_answer_encode}]")
            return _user_id, _result, _question_encode, _answer_encode

        callback_query = update.callback_query
        user = callback_query.from_user
        message = callback_query.message
        chat = message.chat
        user_id, result, question, answer = await query_callback(callback_query.data)
        Log.info(f"用户 {user.full_name}[{user.id}] 在群 {chat.title}[{chat.id}] 点击Auth认证命令 ")
        if user.id != user_id:
            await callback_query.answer(text="这不是你的验证！\n"
                                             "再乱点再按我叫西风骑士团、千岩军和天领奉行了！", show_alert=True)
            return
        Log.info(f"用户 {user.full_name}[{user.id}] 在群 {chat.title}[{chat.id}] 认证结果为 {'通过' if result else '失败'}")
        if result:
            buttons = [[InlineKeyboardButton("驱离", callback_data=f"auth_admin|kick|{user.id}")]]
            await callback_query.answer(text="验证成功", show_alert=False)
            await self.restore_member(context, chat.id, user_id)
            if schedule := context.job_queue.scheduler.get_job(f"{chat.id}|{user.id}|auth_clean_join_message"):
                schedule.remove()
            text = f"{user.mention_markdown_v2()} 验证成功，向着星辰与深渊！\n" \
                   f"问题：{escape_markdown(question, version=2)} \n" \
                   f"回答：{escape_markdown(answer, version=2)}"
            Log.info(f"用户 user_id[{user_id}] 在群 {chat.title}[{chat.id}] 验证成功")
        else:
            buttons = [[InlineKeyboardButton("驱离", callback_data=f"auth_admin|kick|{user.id}"),
                        InlineKeyboardButton("撤回驱离", callback_data=f"auth_admin|unban|{user.id}")]]
            await callback_query.answer(text=f"验证失败，请在 {self.time_out} 秒后重试", show_alert=True)
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=user_id,
                                              until_date=int(time.time()) + self.kick_time)
            text = f"{user.mention_markdown_v2()} 验证失败，已经赶出提瓦特大陆！\n" \
                   f"问题：{escape_markdown(question, version=2)} \n" \
                   f"回答：{escape_markdown(answer, version=2)}"
            Log.info(f"用户 user_id[{user_id}] 在群 {chat.title}[{chat.id}] 验证失败")
        try:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as exc:
            if 'are exactly the same as ' in str(exc):
                Log.warning("编辑消息发生异常，可能为用户点按多次键盘导致")
            else:
                raise exc
        if schedule := context.job_queue.scheduler.get_job(f"{chat.id}|{user.id}|auth_kick"):
            schedule.remove()

    async def new_mem(self, update: Update, context: CallbackContext) -> None:
        await self.refresh_quiz()
        message = update.message
        chat = message.chat
        for user in message.new_chat_members:
            if user.id == context.bot.id:
                return
            Log.info(f"用户 {user.full_name}[{user.id}] 尝试加入群 {chat.title}[{chat.id}]")
        not_enough_rights = context.chat_data.get("not_enough_rights", False)
        if not_enough_rights:
            return
        chat_administrators = await self.get_chat_administrators(context, chat_id=chat.id)
        if self.is_admin(chat_administrators, message.from_user.id):
            await message.reply_text("派蒙检测到管理员邀请，自动放行了！")
            return
        for user in message.new_chat_members:
            if user.is_bot:
                continue
            question_id_list = await self.quiz_service.get_question_id_list()
            if len(question_id_list) == 0:
                await message.reply_text("旅行者！！！派蒙的问题清单你还没给我！！快去私聊我给我问题！")
                return
            try:
                await context.bot.restrict_chat_member(chat_id=message.chat.id, user_id=user.id,
                                                       permissions=ChatPermissions(can_send_messages=False))
            except BadRequest as err:
                if "Not enough rights" in str(err):
                    Log.warning(f"权限不够 chat_id[{message.chat_id}]")
                    # reply_message = await message.reply_markdown_v2(f"派蒙无法修改 {user.mention_markdown_v2()} 的权限！"
                    #                                                 f"请检查是否给派蒙授权管理了")
                    context.chat_data["not_enough_rights"] = True
                    # await context.bot.delete_message(chat.id, reply_message.message_id)
                    return
                else:
                    raise err
            index = self.random.random(0, len(question_id_list))
            question = await self.quiz_service.get_question(question_id_list[index])
            buttons = [
                [
                    InlineKeyboardButton(
                        answer.text,
                        callback_data=f"auth_challenge|{user.id}|{question['question_id']}|{answer['answer_id']}",
                    )
                ]
                for answer in question.answers
            ]
            random.shuffle(buttons)
            buttons.append(
                [
                    InlineKeyboardButton(
                        "放行",
                        callback_data=f"auth_admin|pass|{user.id}",
                    ),
                    InlineKeyboardButton(
                        "驱离",
                        callback_data=f"auth_admin|kick|{user.id}",
                    ),
                ]
            )
            reply_message = f"*欢迎来到「提瓦特」世界！* \n" \
                            f"问题: {escape_markdown(question.text, version=2)} \n" \
                            f"请在 {self.time_out}S 内回答问题"
            Log.debug(f"发送入群验证问题 question_id[{question.question_id}] question[{question.text}] \n"
                      f"给{user.full_name}[{user.id}] 在 {chat.title}[{chat.id}]")
            try:
                question_message = await message.reply_markdown_v2(reply_message,
                                                                   reply_markup=InlineKeyboardMarkup(buttons))
            except BadRequest as error:
                await message.reply_text("派蒙分心了一下，不小心忘记你了，你只能先退出群再重新进来吧。")
                raise error
            context.job_queue.run_once(callback=self.kick_member_job, when=self.time_out,
                                       name=f"{chat.id}|{user.id}|auth_kick", chat_id=chat.id, user_id=user.id,
                                       job_kwargs={"replace_existing": True, "id": f"{chat.id}|{user.id}|auth_kick"})
            context.job_queue.run_once(callback=self.clean_message_job, when=self.time_out, data=message.message_id,
                                       name=f"{chat.id}|{user.id}|auth_clean_join_message",
                                       chat_id=chat.id, user_id=user.id,
                                       job_kwargs={"replace_existing": True, "id": f"{chat.id}|{user.id}|auth_kick"})
            context.job_queue.run_once(callback=self.clean_message_job, when=self.time_out,
                                       data=question_message.message_id,
                                       name=f"{chat.id}|{user.id}|auth_clean_question_message",
                                       chat_id=chat.id, user_id=user.id,
                                       job_kwargs={"replace_existing": True, "id": f"{chat.id}|{user.id}|auth_kick"})