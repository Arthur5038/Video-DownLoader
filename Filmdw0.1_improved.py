import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import requests
import os
import logging
import m3u8
import subprocess
from urllib.parse import urljoin, urlparse
import time
from typing import Optional, Callable
from dataclasses import dataclass
from pathlib import Path
import sys
import queue


# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('video_downloader.log'),
        logging.StreamHandler()
    ]
)

@dataclass
class DownloadConfig:
    """Конфигурация для загрузки"""
    max_retries: int = 5
    retry_delay: int = 5
    timeout: int = 30
    chunk_size: int = 8192
    segment_timeout: int = 10

class DownloadManager:
    """Менеджер загрузки с поддержкой паузы и остановки"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self.is_paused = False
        self.is_stopped = False
        self.download_thread: Optional[threading.Thread] = None
        self.progress_callback: Optional[Callable] = None
        
    def pause(self):
        """Приостановить загрузку"""
        self.is_paused = True
        logging.info("Загрузка приостановлена")
        
    def resume(self):
        """Возобновить загрузку"""
        self.is_paused = False
        logging.info("Загрузка возобновлена")
        
    def stop(self):
        """Остановить загрузку (без блокировки GUI)"""
        self.is_stopped = True
        self.is_paused = False
        # Не блокируем главный поток ожиданием join; поток отмечаем как фоновой при создании
        logging.info("Загрузка остановлена")
        
    def wait_if_paused(self):
        """Ожидание при паузе"""
        while self.is_paused and not self.is_stopped:
            time.sleep(0.01)  # Уменьшаем время ожидания для лучшей отзывчивости
            
    def is_stopped_or_paused(self) -> bool:
        """Проверка остановки или паузы"""
        return self.is_stopped or self.is_paused

class VideoDownloader:
    """Класс для загрузки видео"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self.download_manager = DownloadManager(config)
        
    def download_segment(self, segment_url: str, segment_file: Path, 
                        segment_index: int, total_segments: int) -> bool:
        """Загрузка одного сегмента с повторными попытками"""
        
        for attempt in range(self.config.max_retries):
            try:
                self.download_manager.wait_if_paused()
                
                if self.download_manager.is_stopped:
                    return False
                    
                if segment_file.exists():
                    logging.info(f"Сегмент {segment_index + 1}/{total_segments}: уже загружен")
                    return True
                
                logging.info(f"Загружаем сегмент {segment_index + 1}/{total_segments}: {segment_url}")
                
                response = requests.get(
                    segment_url, 
                    timeout=self.config.segment_timeout,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                )
                response.raise_for_status()
                
                segment_file.write_bytes(response.content)
                return True
                
            except requests.exceptions.RequestException as e:
                logging.warning(f"Ошибка загрузки сегмента {segment_url}: {e}. "
                              f"Попытка {attempt + 1}/{self.config.max_retries}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay)
                    
        logging.error(f"Не удалось загрузить сегмент {segment_url} после {self.config.max_retries} попыток")
        return False
    
    def download_m3u8_video(self, playlist_url: str, output_dir: Path, 
                           progress_callback: Optional[Callable] = None) -> bool:
        """Загрузка M3U8 видео"""
        try:
            # Получаем ID видео из URL
            video_id = self._extract_video_id(playlist_url)
            logging.info(f"ID видео: {video_id}")
            
            # Создаем структуру папок
            video_dir = output_dir / video_id
            segments_dir = video_dir / 'segments'
            video_dir.mkdir(parents=True, exist_ok=True)
            segments_dir.mkdir(exist_ok=True)
            
            # Загружаем плейлист
            response = requests.get(playlist_url, timeout=self.config.timeout)
            response.raise_for_status()
            
            m3u8_obj = m3u8.loads(response.text)
            total_segments = len(m3u8_obj.segments)
            
            if total_segments == 0:
                logging.error("Плейлист не содержит сегментов")
                return False
            
            # Загружаем сегменты
            for i, segment in enumerate(m3u8_obj.segments):
                if self.download_manager.is_stopped:
                    return False
                    
                segment_url = urljoin(playlist_url, segment.uri)
                segment_file = segments_dir / f"segment_{i:04d}.ts"
                
                success = self.download_segment(segment_url, segment_file, i, total_segments)
                if not success:
                    return False
                
                # Обновляем прогресс
                if progress_callback:
                    progress_callback(i + 1, total_segments)
            
            # Объединяем сегменты
            return self._merge_segments(video_dir, segments_dir, total_segments)
            
        except Exception as e:
            logging.error(f"Ошибка при загрузке M3U8 видео: {e}")
            return False
    
    def download_mp4_video(self, video_url: str, output_dir: Path, 
                          progress_callback: Optional[Callable] = None) -> bool:
        """Загрузка MP4 видео"""
        try:
            video_id = self._extract_video_id(video_url)
            video_dir = output_dir / video_id
            video_dir.mkdir(parents=True, exist_ok=True)
            
            video_path = video_dir / 'output.mp4'
            
            response = requests.get(
                video_url, 
                stream=True, 
                timeout=self.config.timeout,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0
            
            with open(video_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                    if self.download_manager.is_stopped:
                        return False
                        
                    self.download_manager.wait_if_paused()
                    
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded_size, total_size)
            
            logging.info(f"MP4 видео успешно загружено: {video_path}")
            return True
            
        except Exception as e:
            logging.error(f"Ошибка при загрузке MP4 видео: {e}")
            return False
    
    def _extract_video_id(self, url: str) -> str:
        """Извлечение ID видео из URL"""
        try:
            parsed = urlparse(url)
            path_parts = parsed.path.strip('/').split('/')
            if len(path_parts) >= 3:
                return path_parts[-3]
            elif len(path_parts) >= 1:
                return path_parts[-1].split('.')[0]
            else:
                return f"video_{int(time.time())}"
        except Exception:
            return f"video_{int(time.time())}"
    
    def _merge_segments(self, video_dir: Path, segments_dir: Path, 
                       total_segments: int) -> bool:
        """Объединение сегментов в единый файл"""
        try:
            output_path = video_dir / 'output.mp4'
            
            if output_path.exists():
                logging.info(f"Выходной файл уже существует: {output_path}")
                return True
            
            # Создаем список файлов для FFmpeg
            filelist_path = video_dir / 'filelist.txt'
            with open(filelist_path, 'w', encoding='utf-8') as f:
                for i in range(total_segments):
                    segment_path = segments_dir / f'segment_{i:04d}.ts'
                    if segment_path.exists():
                        f.write(f"file '{segment_path.absolute()}'\n")
                    else:
                        logging.warning(f"Сегмент отсутствует: {segment_path}")
            
            # Запускаем FFmpeg
            ffmpeg_cmd = [
                'ffmpeg', '-f', 'concat', '-safe', '0', 
                '-i', str(filelist_path), '-c', 'copy', str(output_path),
                '-y'  # Перезаписывать существующий файл
            ]
            
            result = subprocess.run(
                ffmpeg_cmd, 
                capture_output=True, 
                text=True, 
                timeout=300  # 5 минут таймаут
            )
            
            if result.returncode == 0:
                logging.info(f"Видео успешно объединено: {output_path}")
                # Удаляем временные файлы
                filelist_path.unlink(missing_ok=True)
                return True
            else:
                logging.error(f"Ошибка FFmpeg: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            logging.error("Таймаут при объединении сегментов")
            return False
        except Exception as e:
            logging.error(f"Ошибка при объединении сегментов: {e}")
            return False

class VideoDownloaderGUI:
    """Графический интерфейс для загрузчика видео"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Видео Загрузчик v2.0")
        self.root.geometry("600x400")
        self.root.resizable(True, True)
        
        # Настройка приоритета обработки событий
        self.root.option_add('*tearOff', False)
        
        # Конфигурация
        self.config = DownloadConfig()
        self.downloader = VideoDownloader(self.config)
        
        # Переменные
        self.url_var = tk.StringVar()
        self.dir_var = tk.StringVar()
        self.progress_var = tk.StringVar(value="Готов к загрузке")
        
        # Флаг для предотвращения множественных загрузок
        self.is_downloading = False
        
        self._setup_ui()
        self._center_window()
        
        # Обработчик закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        # Добавляем обработчики событий для улучшения отзывчивости
        self.root.bind('<Control-r>', self._clear_fields)  # Ctrl+R для очистки
        self.root.bind('<Control-l>', lambda e: self.url_entry.focus_set())  # Ctrl+L для фокуса на URL
        self.root.bind('<Control-d>', lambda e: self.dir_entry.focus_set())  # Ctrl+D для фокуса на директорию
        
    def _setup_ui(self):
        """Настройка пользовательского интерфейса"""
        # Основной фрейм
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Настройка весов для растягивания
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        
        # URL
        ttk.Label(main_frame, text="URL видео:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.url_entry = ttk.Entry(main_frame, textvariable=self.url_var, width=50)
        self.url_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(5, 0), pady=5)
        # Добавляем обработчик событий для поля URL
        self.url_entry.bind('<FocusIn>', self._on_url_focus)
        self.url_entry.bind('<Return>', lambda e: self.dir_entry.focus_set())
        
        # Директория
        ttk.Label(main_frame, text="Папка сохранения:").grid(row=1, column=0, sticky=tk.W, pady=5)
        dir_frame = ttk.Frame(main_frame)
        dir_frame.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(5, 0), pady=5)
        dir_frame.columnconfigure(0, weight=1)
        
        self.dir_entry = ttk.Entry(dir_frame, textvariable=self.dir_var)
        self.dir_entry.grid(row=0, column=0, sticky=(tk.W, tk.E))
        # Добавляем обработчик событий для поля директории
        self.dir_entry.bind('<FocusIn>', self._on_dir_focus)
        self.dir_entry.bind('<Return>', lambda e: self._start_download())
        
        ttk.Button(dir_frame, text="Выбрать", command=self._select_directory).grid(row=0, column=1, padx=(5, 0))
        
        # Прогресс
        ttk.Label(main_frame, text="Прогресс:").grid(row=2, column=0, sticky=tk.W, pady=(20, 5))
        
        self.progress_label = ttk.Label(main_frame, textvariable=self.progress_var)
        self.progress_label.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=(5, 0), pady=(20, 5))
        
        self.progress_bar = ttk.Progressbar(main_frame, mode='determinate', length=400)
        self.progress_bar.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)
        
        # Кнопки управления
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=4, column=0, columnspan=2, pady=20)
        
        self.start_btn = ttk.Button(button_frame, text="Начать загрузку", command=self._start_download)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.pause_btn = ttk.Button(button_frame, text="Пауза", command=self._pause_download, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=5)
        
        self.resume_btn = ttk.Button(button_frame, text="Продолжить", command=self._resume_download, state=tk.DISABLED)
        self.resume_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(button_frame, text="Остановить", command=self._stop_download, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        # Кнопка очистки полей
        clear_btn = ttk.Button(button_frame, text="Очистить", command=self._clear_fields)
        clear_btn.pack(side=tk.LEFT, padx=5)
        
        # Статус
        self.status_label = ttk.Label(main_frame, text="Готов к работе", foreground="green")
        self.status_label.grid(row=5, column=0, columnspan=2, pady=10)
        
        # Подсказки
        hints_frame = ttk.Frame(main_frame)
        hints_frame.grid(row=6, column=0, columnspan=2, pady=5)
        
        hints_text = "Горячие клавиши: Ctrl+L (URL), Ctrl+D (папка), Ctrl+R (очистить), Enter (следующее поле)"
        hints_label = ttk.Label(hints_frame, text=hints_text, font=("TkDefaultFont", 8), foreground="gray")
        hints_label.pack()
        
    def _center_window(self):
        """Центрирование окна"""
        # Откладываем центрирование до полной инициализации окна
        self.root.after(100, self._do_center_window)
    
    def _do_center_window(self):
        """Выполнение центрирования окна"""
        try:
            self.root.update_idletasks()
            width = self.root.winfo_width()
            height = self.root.winfo_height()
            x = (self.root.winfo_screenwidth() // 2) - (width // 2)
            y = (self.root.winfo_screenheight() // 2) - (height // 2)
            self.root.geometry(f'{width}x{height}+{x}+{y}')
        except tk.TclError:
            pass
    
    def _select_directory(self):
        """Выбор директории"""
        try:
            directory = filedialog.askdirectory()
            if directory:
                self.dir_var.set(directory)
                # Возвращаем фокус на поле URL после выбора директории
                self.root.after(100, lambda: self.url_entry.focus_set())
        except tk.TclError:
            # Игнорируем ошибки при закрытии диалога
            pass
    
    def _post_progress(self, current: int, total: int):
        """Получено из фонового потока: делегируем обновление в главный поток"""
        # Используем более эффективный способ обновления UI
        try:
            self.root.after_idle(lambda: self._update_progress_ui(current, total))
        except tk.TclError:
            # Окно закрыто, игнорируем обновление
            pass

    def _update_progress_ui(self, current: int, total: int):
        """Обновление прогресса (главный поток Tk)"""
        try:
            percentage = (current / total) * 100 if total else 0
            self.progress_bar['value'] = percentage
            self.progress_var.set(f"Загружено: {current}/{total} ({percentage:.1f}%)")
        except tk.TclError:
            # Окно закрыто, игнорируем обновление
            pass
    
    def _start_download(self):
        """Начать загрузку"""
        try:
            # Предотвращаем множественные загрузки
            if self.is_downloading:
                return
                
            url = self.url_var.get().strip()
            directory = self.dir_var.get().strip()
            
            if not url:
                messagebox.showerror("Ошибка", "Введите URL видео")
                self.url_entry.focus_set()
                return
                
            if not directory:
                messagebox.showerror("Ошибка", "Выберите папку для сохранения")
                self.dir_entry.focus_set()
                return
            
            if not Path(directory).exists():
                messagebox.showerror("Ошибка", "Выбранная папка не существует")
                self.dir_entry.focus_set()
                return
            
            # Определяем тип файла
            file_extension = Path(url).suffix.lower()
            
            if file_extension not in ['.m3u8', '.mp4']:
                messagebox.showerror("Ошибка", "Неподдерживаемый формат. Поддерживаются только .m3u8 и .mp4")
                self.url_entry.focus_set()
                return
            
            # Обновляем UI
            self.is_downloading = True
            self._set_download_mode(True)
            self.status_label.config(text="Загрузка...", foreground="blue")
            
            # Запускаем загрузку в отдельном потоке
            self.downloader.download_manager.progress_callback = self._post_progress

            if file_extension == '.m3u8':
                self.downloader.download_manager.download_thread = threading.Thread(
                    target=self._download_m3u8_wrapper, args=(url, directory), daemon=True
                )
            else:
                self.downloader.download_manager.download_thread = threading.Thread(
                    target=self._download_mp4_wrapper, args=(url, directory), daemon=True
                )

            self.downloader.download_manager.download_thread.start()
        except Exception as e:
            logging.error(f"Ошибка при запуске загрузки: {e}")
            messagebox.showerror("Ошибка", f"Не удалось запустить загрузку: {e}")
            self.is_downloading = False
            self._set_download_mode(False)
    
    def _download_m3u8_wrapper(self, url: str, directory: str):
        """Обертка для загрузки M3U8"""
        try:
            success = self.downloader.download_m3u8_video(url, Path(directory), self._post_progress)
            self._download_finished(success, "M3U8 видео")
        except Exception as e:
            logging.error(f"Ошибка в потоке загрузки M3U8: {e}")
            self._download_finished(False, "M3U8 видео")
    
    def _download_mp4_wrapper(self, url: str, directory: str):
        """Обертка для загрузки MP4"""
        try:
            success = self.downloader.download_mp4_video(url, Path(directory), self._post_progress)
            self._download_finished(success, "MP4 видео")
        except Exception as e:
            logging.error(f"Ошибка в потоке загрузки MP4: {e}")
            self._download_finished(False, "MP4 видео")
    
    def _download_finished(self, success: bool, video_type: str):
        """Обработка завершения загрузки"""
        try:
            self.is_downloading = False
            self.root.after_idle(lambda: self._set_download_mode(False))
            
            if success:
                self.root.after_idle(lambda: self.status_label.config(text="Загрузка завершена успешно!", foreground="green"))
                self.root.after_idle(lambda: messagebox.showinfo("Успех", f"{video_type} успешно загружено!"))
            else:
                self.root.after_idle(lambda: self.status_label.config(text="Ошибка загрузки", foreground="red"))
                self.root.after_idle(lambda: messagebox.showerror("Ошибка", f"Не удалось загрузить {video_type}"))
        except tk.TclError:
            # Окно закрыто, игнорируем обновление
            pass
    
    def _set_download_mode(self, downloading: bool):
        """Установка режима загрузки"""
        try:
            if downloading:
                self.start_btn.config(state=tk.DISABLED)
                self.pause_btn.config(state=tk.NORMAL)
                self.stop_btn.config(state=tk.NORMAL)
                # Блокируем поля ввода во время загрузки
                self.url_entry.config(state=tk.DISABLED)
                self.dir_entry.config(state=tk.DISABLED)
            else:
                self.start_btn.config(state=tk.NORMAL)
                self.pause_btn.config(state=tk.DISABLED)
                self.resume_btn.config(state=tk.DISABLED)
                self.stop_btn.config(state=tk.DISABLED)
                self.progress_bar['value'] = 0
                self.progress_var.set("Готов к загрузке")
                # Разблокируем поля ввода после загрузки
                self.url_entry.config(state=tk.NORMAL)
                self.dir_entry.config(state=tk.NORMAL)
                # Возвращаем фокус на поле URL
                self.url_entry.focus_set()
        except tk.TclError:
            # Окно закрыто, игнорируем обновление
            pass
    
    def _pause_download(self):
        """Приостановить загрузку"""
        try:
            self.downloader.download_manager.pause()
            self.pause_btn.config(state=tk.DISABLED)
            self.resume_btn.config(state=tk.NORMAL)
            self.status_label.config(text="Загрузка приостановлена", foreground="orange")
        except Exception as e:
            logging.error(f"Ошибка при паузе: {e}")
    
    def _resume_download(self):
        """Возобновить загрузку"""
        try:
            self.downloader.download_manager.resume()
            self.pause_btn.config(state=tk.NORMAL)
            self.resume_btn.config(state=tk.DISABLED)
            self.status_label.config(text="Загрузка...", foreground="blue")
        except Exception as e:
            logging.error(f"Ошибка при возобновлении: {e}")
    
    def _stop_download(self):
        """Остановить загрузку"""
        try:
            self.downloader.download_manager.stop()
            self._set_download_mode(False)
            self.status_label.config(text="Загрузка остановлена", foreground="red")
        except Exception as e:
            logging.error(f"Ошибка при остановке: {e}")
    
    def _on_url_focus(self, event=None):
        """Обработчик фокуса на поле URL"""
        try:
            if not self.is_downloading:
                self.status_label.config(text="Введите URL видео", foreground="blue")
        except Exception as e:
            logging.error(f"Ошибка при фокусе на URL: {e}")
    
    def _on_dir_focus(self, event=None):
        """Обработчик фокуса на поле директории"""
        try:
            if not self.is_downloading:
                self.status_label.config(text="Выберите папку для сохранения", foreground="blue")
        except Exception as e:
            logging.error(f"Ошибка при фокусе на директорию: {e}")
    
    def _clear_fields(self, event=None):
        """Очистка полей ввода"""
        try:
            if not self.is_downloading:
                self.url_var.set("")
                self.dir_var.set("")
                self.url_entry.focus_set()
                self.status_label.config(text="Поля очищены", foreground="blue")
        except Exception as e:
            logging.error(f"Ошибка при очистке полей: {e}")
    
    def _on_closing(self):
        """Обработчик закрытия окна"""
        try:
            if self.is_downloading:
                self.downloader.download_manager.stop()
            self.root.destroy()
        except Exception as e:
            logging.error(f"Ошибка при закрытии окна: {e}")
            self.root.destroy()
    
    def run(self):
        """Запуск приложения"""
        self.root.mainloop()

def main():
    """Главная функция"""
    try:
        app = VideoDownloaderGUI()
        app.run()
    except Exception as e:
        logging.error(f"Критическая ошибка приложения: {e}")
        messagebox.showerror("Критическая ошибка", f"Произошла ошибка: {e}")

if __name__ == "__main__":
    main()
