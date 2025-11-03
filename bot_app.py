"""Telegram bot GUI wrapper for decoding DataMatrix codes with Aspose Barcode Cloud."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from aspose_barcode_cloud.api.recognize_api import RecognizeApi  # type: ignore
from aspose_barcode_cloud.apis.barcode_api import BarcodeApi  # type: ignore
from aspose_barcode_cloud.api_client import ApiClient  # type: ignore
from aspose_barcode_cloud.configuration import Configuration  # type: ignore
from aspose_barcode_cloud.models.decode_barcode_type import DecodeBarcodeType  # type: ignore
from aspose_barcode_cloud.rest import ApiException  # type: ignore
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

LOGGER = logging.getLogger("datamatrix_bot")


class TkinterLogHandler(logging.Handler):
    """Logging handler that streams log records into a Tkinter text widget."""

    def __init__(self, text_widget: scrolledtext.ScrolledText) -> None:
        super().__init__()
        self._text_widget = text_widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._text_widget.after(0, self._append_message, msg)

    def _append_message(self, message: str) -> None:
        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.insert(tk.END, message + "\n")
        self._text_widget.configure(state=tk.DISABLED)
        self._text_widget.yview_moveto(1.0)


class AsposeDecoder:
    """Client wrapper for Aspose Barcode Cloud decoding."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        configuration = Configuration(client_id=client_id, client_secret=client_secret)
        api_client = ApiClient(configuration=configuration)
        self._api = RecognizeApi(api_client)

    def decode_datamatrix(self, image_bytes: bytes) -> List[Dict[str, Optional[str]]]:
        """Decode DataMatrix barcodes in the provided image data."""
        response = self._api.recognize_multipart(
            barcode_type=DecodeBarcodeType.DATAMATRIX,
            file=bytearray(image_bytes),
        )

        barcodes: List[Dict[str, Optional[str]]] = []
        if response is None:
            return barcodes

        for item in getattr(response, "barcodes", []) or []:
            value = getattr(item, "barcode_value", None)
            type_name = getattr(item, "type", None)
            confidence = getattr(item, "confidence", None)
            barcodes.append(
                {
                    "value": value,
                    "type": type_name,
                    "confidence": confidence,
                }
            )
        return barcodes


@dataclass
class UserStats:
    username: str
    first_name: str
    last_name: str
    request_count: int = 0


class StatsManager:
    """Tracks user interaction statistics and notifies listeners on update."""

    def __init__(self) -> None:
        self._stats: Dict[int, UserStats] = {}
        self._lock = threading.Lock()
        self._listeners: List = []

    def record_request(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> None:
        with self._lock:
            stats = self._stats.setdefault(
                user_id,
                UserStats(
                    username=username or "",
                    first_name=first_name or "",
                    last_name=last_name or "",
                ),
            )
            stats.username = username or stats.username
            stats.first_name = first_name or stats.first_name
            stats.last_name = last_name or stats.last_name
            stats.request_count += 1
            snapshot = self._snapshot_locked()

        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("Admin panel listener raised an exception")

    def snapshot(self) -> Dict[int, UserStats]:
        with self._lock:
            return self._snapshot_locked()

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    def _snapshot_locked(self) -> Dict[int, UserStats]:
        return {user_id: UserStats(**vars(data)) for user_id, data in self._stats.items()}


class BotRunner(threading.Thread):
    """Background thread responsible for running the Telegram bot."""

    def __init__(self, token: str, decoder: AsposeDecoder, stats: StatsManager) -> None:
        super().__init__(daemon=True)
        self._token = token
        self._decoder = decoder
        self._stats = stats
        self._application: Optional[Application] = None

    def run(self) -> None:  # pragma: no cover - requires Telegram network
        try:
            LOGGER.info("Starting Telegram bot polling loop")
            application = (
                ApplicationBuilder()
                .token(self._token)
                .rate_limiter(AIORateLimiter())
                .post_init(self._post_init)
                .build()
            )

            application.bot_data["decoder"] = self._decoder
            application.bot_data["stats"] = self._stats

            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("stats", stats_command))
            application.add_handler(
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE | filters.Document.PNG | filters.Document.JPEG,
                    image_handler,
                )
            )

            application.add_error_handler(error_handler)
            self._application = application
            application.run_polling(stop_signals=None)
        except Exception:  # pragma: no cover - network failures
            LOGGER.exception("Telegram bot terminated due to an error")

    async def _post_init(self, application: Application) -> None:
        bot_user = await application.bot.get_me()
        LOGGER.info("Bot authorised as @%s", bot_user.username)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Здравствуйте! Отправьте фотографию или изображение с DataMatrix кодом, и я распознаю его содержимое."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "1. Отправьте фото или изображение с DataMatrix кодом.\n"
        "2. Дождитесь ответа с декодированными данными.\n"
        "3. Используйте команду /stats для просмотра количества ваших запросов."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    stats: StatsManager = context.application.bot_data["stats"]
    snapshot = stats.snapshot()
    user_stats = snapshot.get(user.id)
    if not user_stats:
        await update.message.reply_text("Вы ещё не отправляли DataMatrix коды.")
        return

    await update.message.reply_text(
        f"Вы отправили {user_stats.request_count} изображений. Спасибо, {user_stats.first_name or user.first_name or 'пользователь'}!"
    )


async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    user = update.effective_user
    user_label = f"{user.id} ({user.username or user.full_name})"
    LOGGER.info("Получено изображение от пользователя %s", user_label)

    try:
        await message.chat.send_action(ChatAction.TYPING)
    except Exception:  # pragma: no cover - best effort
        LOGGER.debug("Не удалось отправить индикатор набора текста", exc_info=True)

    file = None
    if message.photo:
        file = await message.photo[-1].get_file()
    elif message.document:
        file = await message.document.get_file()

    if file is None:
        await message.reply_text("Пожалуйста, отправьте фотографию или изображение с DataMatrix кодом.")
        LOGGER.warning("Получено неподдерживаемое сообщение от пользователя %s", user_label)
        return

    try:
        file_bytes = await file.download_as_bytearray()
        decoder: AsposeDecoder = context.application.bot_data["decoder"]
        LOGGER.info("Запрос к Aspose Barcode Cloud для пользователя %s", user_label)
        barcodes = await asyncio.to_thread(decoder.decode_datamatrix, bytes(file_bytes))
    except ApiException as api_error:
        LOGGER.exception("Ошибка Aspose Barcode Cloud: %s", api_error)
        await message.reply_text("Произошла ошибка при обращении к сервису Aspose Barcode Cloud. Попробуйте позже.")
        return
    except Exception as exc:
        LOGGER.exception("Не удалось обработать изображение: %s", exc)
        await message.reply_text("Не удалось обработать изображение. Убедитесь, что это корректный DataMatrix код.")
        return

    if not barcodes:
        LOGGER.info("DataMatrix код не найден для пользователя %s", user_label)
        await message.reply_text("DataMatrix код не найден. Попробуйте отправить изображение лучшего качества.")
        return

    stats: StatsManager = context.application.bot_data["stats"]
    stats.record_request(user.id, user.username, user.first_name, user.last_name)

    response_lines = [
        "Результаты распознавания DataMatrix:" 
    ]
    for idx, barcode in enumerate(barcodes, start=1):
        value = barcode.get("value") or "<пусто>"
        type_name = barcode.get("type") or "DataMatrix"
        confidence = barcode.get("confidence")
        confidence_text = f" (доверие: {confidence:.2f})" if isinstance(confidence, float) else ""
        response_lines.append(f"{idx}. [{type_name}]{confidence_text}: {value}")

    LOGGER.info("Успешно распознано %d DataMatrix код(ов) для пользователя %s", len(barcodes), user_label)
    await message.reply_text("\n".join(response_lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled error while processing update: %s", context.error)


class AdminPanel:
    """Simple admin panel displaying bot usage statistics."""

    def __init__(self, root: tk.Tk, stats: StatsManager) -> None:
        self._root = root
        self._window: Optional[tk.Toplevel] = None
        self._stats = stats
        self._tree: Optional[ttk.Treeview] = None
        self._total_label: Optional[ttk.Label] = None

    def show(self, snapshot: Dict[int, UserStats]) -> None:
        if self._window and tk.Toplevel.winfo_exists(self._window):
            self._window.lift()
            self._refresh(snapshot)
            return

        self._window = tk.Toplevel(self._root)
        self._window.title("Админ панель - Статистика пользователей")
        self._window.geometry("620x320")

        columns = ("username", "first_name", "last_name", "count")
        self._tree = ttk.Treeview(self._window, columns=columns, show="headings")
        self._tree.heading("username", text="Username")
        self._tree.heading("first_name", text="Имя")
        self._tree.heading("last_name", text="Фамилия")
        self._tree.heading("count", text="Запросы")
        self._tree.column("username", width=150)
        self._tree.column("first_name", width=150)
        self._tree.column("last_name", width=150)
        self._tree.column("count", width=100, anchor=tk.CENTER)
        self._tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

        scrollbar = ttk.Scrollbar(self._window, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._total_label = ttk.Label(self._window, text="Всего пользователей: 0 | Всего запросов: 0")
        self._total_label.pack(fill=tk.X, padx=10, pady=10)

        self._refresh(snapshot)

    def _refresh(self, snapshot: Dict[int, UserStats]) -> None:
        if not self._tree:
            return

        for item in self._tree.get_children():
            self._tree.delete(item)

        total_requests = 0
        for user_id, stats in snapshot.items():
            username = stats.username or "-"
            first_name = stats.first_name or "-"
            last_name = stats.last_name or "-"
            total_requests += stats.request_count
            self._tree.insert(
                "",
                tk.END,
                iid=str(user_id),
                values=(username, first_name, last_name, stats.request_count),
            )

        if self._total_label:
            self._total_label.configure(
                text=f"Всего пользователей: {len(snapshot)} | Всего запросов: {total_requests}"
            )


class BotApp:
    """Tkinter desktop application that manages the Telegram bot lifecycle."""

    def __init__(self) -> None:
        self._root = tk.Tk()
        self._root.title("DataMatrix Bot Manager")
        self._root.geometry("700x520")

        self._stats = StatsManager()
        self._stats.add_listener(self._on_stats_updated)
        self._admin_panel = AdminPanel(self._root, self._stats)
        self._bot_thread: Optional[BotRunner] = None

        self._build_ui()

    def _build_ui(self) -> None:
        form_frame = ttk.Frame(self._root)
        form_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(form_frame, text="Telegram Bot Token:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self._token_entry = ttk.Entry(form_frame)
        self._token_entry.grid(row=0, column=1, sticky=tk.EW, pady=5)

        ttk.Label(form_frame, text="Aspose Client ID:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self._client_id_entry = ttk.Entry(form_frame)
        self._client_id_entry.grid(row=1, column=1, sticky=tk.EW, pady=5)

        ttk.Label(form_frame, text="Aspose Client Secret:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self._client_secret_entry = ttk.Entry(form_frame, show="*")
        self._client_secret_entry.grid(row=2, column=1, sticky=tk.EW, pady=5)

        form_frame.columnconfigure(1, weight=1)

        button_frame = ttk.Frame(self._root)
        button_frame.pack(fill=tk.X, padx=10)

        self._start_button = ttk.Button(button_frame, text="Запустить бота", command=self._start_bot)
        self._start_button.pack(side=tk.LEFT, padx=(0, 10))

        self._admin_button = ttk.Button(button_frame, text="Админ панель", command=self._show_admin_panel, state=tk.DISABLED)
        self._admin_button.pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(self._root, text="Логи")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self._log_widget = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED, wrap=tk.WORD)
        self._log_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        handler = TkinterLogHandler(self._log_widget)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _start_bot(self) -> None:
        if self._bot_thread is not None:
            messagebox.showinfo("Бот уже запущен", "Телеграм бот уже работает.")
            return

        token = self._token_entry.get().strip()
        client_id = self._client_id_entry.get().strip()
        client_secret = self._client_secret_entry.get().strip()

        if not token or not client_id or not client_secret:
            messagebox.showerror("Недостаточно данных", "Пожалуйста, заполните все поля с настройками.")
            return

        try:
            decoder = AsposeDecoder(client_id=client_id, client_secret=client_secret)
        except Exception as exc:
            LOGGER.exception("Не удалось инициализировать Aspose Barcode Cloud: %s", exc)
            messagebox.showerror("Ошибка", "Не удалось подключиться к Aspose Barcode Cloud. Проверьте ключи.")
            return

        self._bot_thread = BotRunner(token=token, decoder=decoder, stats=self._stats)
        self._bot_thread.start()
        LOGGER.info("Запрошен запуск Telegram бота")

        self._start_button.configure(state=tk.DISABLED)
        self._token_entry.configure(state=tk.DISABLED)
        self._client_id_entry.configure(state=tk.DISABLED)
        self._client_secret_entry.configure(state=tk.DISABLED)
        self._admin_button.configure(state=tk.NORMAL)

    def _show_admin_panel(self) -> None:
        snapshot = self._stats.snapshot()
        self._admin_panel.show(snapshot)

    def _on_stats_updated(self, snapshot: Dict[int, UserStats]) -> None:
        self._root.after(0, self._admin_panel._refresh, snapshot)

    def _on_close(self) -> None:
        LOGGER.info("Закрытие приложения инициировано пользователем")
        self._root.destroy()

    def run(self) -> None:
        LOGGER.info("Приложение запущено. Ожидается ввод параметров.")
        self._root.mainloop()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = BotApp()
    app.run()


if __name__ == "__main__":
    main()
