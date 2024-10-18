import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import requests
import os
import logging
import m3u8
import subprocess
from urllib.parse import urljoin, urlparse
import time
from tkinter import ttk

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Пауза
is_paused = False
download_thread = None
MAX_RETRIES = 5
RETRY_DELAY = 5  # Задержка в секундах между попытками

# Центрирование окна
def center_window(window):
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    x = (window.winfo_screenwidth() // 2) - (width // 2)
    y = (window.winfo_screenheight() // 2) - (height // 2)
    window.geometry(f'{width}x{height}+{x}+{y}')

root = tk.Tk()
root.title("Видео Загрузчик")

root.after(0, lambda: center_window(root))  # Центрируем окно после загрузки

# Функция для управления паузой
def check_for_pause():
    global is_paused
    while is_paused:
        time.sleep(1)  # Ожидание возобновления

# Функция для выбора директории
def select_directory():
    directory = filedialog.askdirectory()
    entry_dir.delete(0, tk.END)
    entry_dir.insert(0, directory)

# Логика для скачивания сегментов .m3u8
def download_segment(segment_url, segment_file, i, total_segments):
    attempts = 0
    while attempts < MAX_RETRIES:
        try:
            global is_paused
            while is_paused:
                time.sleep(1)  # Ожидание при паузе

            if os.path.exists(segment_file):
                logging.info(f"Сегмент {i + 1} из {total_segments}: уже скачан")
                return True

            logging.info(f"Скачиваем сегмент {i + 1} из {total_segments}: {segment_url}")
            response = requests.get(segment_url, timeout=10)
            response.raise_for_status()

            with open(segment_file, 'wb') as f:
                f.write(response.content)

            return True
        except requests.exceptions.RequestException as e:
            attempts += 1
            logging.warning(f"Ошибка при скачивании {segment_url}: {e}. Попытка {attempts} из {MAX_RETRIES}.")
            time.sleep(RETRY_DELAY)
        except KeyboardInterrupt:
            logging.info("Прерывание скачивания сегмента пользователем.")
            sys.exit(0)

    logging.error(f"Не удалось скачать сегмент {segment_url} после {MAX_RETRIES} попыток.")
    return False

# Функция для объединения сегментов и отображения прогресса
def merge_segments_with_progress(filelist_path, output_video_path):
    ffmpeg_cmd = [
        'ffmpeg', '-f', 'concat', '-safe', '0', '-i', filelist_path,
        '-c', 'copy', output_video_path
    ]

    process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    
    for line in process.stdout:
        if "frame=" in line or "time=" in line or "bitrate=" in line:
            # Обновляем метку с прогрессом
            progress_label.config(text=f"Объединение: {line.strip()}")
            root.update_idletasks()

    process.stdout.close()
    process.wait()

    if process.returncode == 0:
        logging.info(f"Видео успешно собрано и сохранено в: {output_video_path}")
        messagebox.showinfo("Готово", f"Видео собрано: {output_video_path}")
    else:
        logging.error(f"Ошибка при объединении сегментов.")

# Логика для скачивания m3u8
def download_m3u8_video(playlist_url, output_dir):
    try:
        video_id = playlist_url.strip('/').split('/')[-3]
        logging.info(f"Идентификатор видео: {video_id}")

        video_output_dir = os.path.join(output_dir, video_id)
        os.makedirs(video_output_dir, exist_ok=True)
        logging.info(f"Директория для видео: {video_output_dir}")

        segments_dir = os.path.join(video_output_dir, 'segments')
        os.makedirs(segments_dir, exist_ok=True)
        logging.info(f"Директория для сегментов: {segments_dir}")

        response = requests.get(playlist_url, timeout=10)
        response.raise_for_status()

        m3u8_obj = m3u8.loads(response.text)
        total_segments = len(m3u8_obj.segments)

        for i, segment in enumerate(m3u8_obj.segments):
            segment_url = urljoin(playlist_url, segment.uri)
            segment_file = os.path.join(segments_dir, f"segment_{i}.ts")

            success = download_segment(segment_url, segment_file, i, total_segments)
            if not success:
                sys.exit(0)

            # Обновляем прогресс
            update_progress(i + 1, total_segments)

        logging.info("Все сегменты скачаны.")

        output_video_path = os.path.join(video_output_dir, 'output.mp4')
        if os.path.exists(output_video_path):
            logging.info(f"Выходной файл {output_video_path} уже существует. Пропускаем объединение.")
            return

        # Создаем список файлов для объединения
        filelist_path = os.path.join(video_output_dir, 'filelist.txt')
        with open(filelist_path, 'w') as f:
            for i in range(total_segments):
                segment_path = os.path.join(segments_dir, f'segment_{i}.ts')
                if os.path.exists(segment_path):
                    f.write(f"file '{segment_path}'\n")
                else:
                    logging.warning(f"Сегмент {segment_path} отсутствует и будет пропущен при объединении.")

        # Вызываем функцию с прогрессом
        merge_segments_with_progress(filelist_path, output_video_path)

    except KeyboardInterrupt:
        logging.info("Прерывание выполнения программы пользователем.")
        sys.exit(0)

# Логика для скачивания mp4
def download_mp4_video(video_url, output_dir):
    try:
        video_id = urlparse(video_url).path.split('/')[-1].split('.')[0]
        logging.info(f"Идентификатор видео: {video_id}")

        video_output_dir = os.path.join(output_dir, video_id)
        os.makedirs(video_output_dir, exist_ok=True)

        video_path = os.path.join(video_output_dir, 'output.mp4')

        response = requests.get(video_url, stream=True)
        response.raise_for_status()

        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        logging.info(f"Видео успешно скачано и сохранено в: {video_path}")
        messagebox.showinfo("Готово", f"Видео скачано: {video_path}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при скачивании видео: {e}")


# Обновление прогресса
def update_progress(current, total):
    progress_label.config(text=f"Загружено {current} из {total} сегментов")
    progress_bar['value'] = (current / total) * 100
    root.update_idletasks()  # Обновляем интерфейс
    
# Функция что выбрасит исключения
def get_file_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path
    return os.path.splitext(path)[1]

# Функция для старта скачивания
def start_download():
    global is_paused, download_thread
    url = entry_url.get()
    output_dir = entry_dir.get()

    if not url or not output_dir:
        messagebox.showerror("Ошибка", "Введите ссылку и выберите директорию.")
        return

    file_extension = get_file_extension(url)

    # Снимаем фокус с полей ввода
    root.focus()

    if file_extension == '.m3u8':
        download_thread = threading.Thread(target=download_m3u8_video, args=(url, output_dir))
        download_thread.start()
    elif file_extension == '.mp4':
        download_thread = threading.Thread(target=download_mp4_video, args=(url, output_dir))
        download_thread.start()
    else:
        messagebox.showerror("Ошибка", "Неподдерживаемый формат файла.")

# Функция для паузы
def pause_download():
    global is_paused
    is_paused = True
    logging.info("Пауза")

# Функция для продолжения
def resume_download():
    global is_paused
    is_paused = False
    logging.info("Возобновление")

# Функция для остановки загрузки
def stop_download():
    global download_thread, is_paused
    is_paused = False
    if download_thread and download_thread.is_alive():
        download_thread.join(0.1)  # Принудительно завершаем поток
    logging.info("Процесс остановлен.")
    messagebox.showinfo("Загрузка", "Загрузка была остановлена.")

# Поле для ввода URL
label_url = tk.Label(root, text="Введите ссылку на видео:")
label_url.pack()

entry_url = tk.Entry(root, width=50)
entry_url.pack()

# Кнопка для выбора директории
label_dir = tk.Label(root, text="Выберите директорию для сохранения:")
label_dir.pack()

entry_dir = tk.Entry(root, width=50)
entry_dir.pack()

btn_dir = tk.Button(root, text="Выбрать директорию", command=select_directory)
btn_dir.pack()

# Прогрессбар и метка для прогресса
progress_label = tk.Label(root, text="")
progress_label.pack()

progress_bar = ttk.Progressbar(root, orient="horizontal", length=300, mode="determinate")
progress_bar.pack()

# Кнопки для управления загрузкой
btn_start = tk.Button(root, text="Старт", command=start_download)
btn_start.pack()

btn_pause = tk.Button(root, text="Пауза", command=pause_download)
btn_pause.pack()

btn_resume = tk.Button(root, text="Продолжить", command=resume_download)
btn_resume.pack()

btn_stop = tk.Button(root, text="Завершить", command=stop_download)
btn_stop.pack()

# Запускаем основной цикл
root.mainloop()


#Рабочий код приложения для терминала
#Пользуйтель столько если разбираетесь в python

"""
import m3u8
import os
import requests
import logging
import time
import sys
import subprocess
from urllib.parse import urlparse, urljoin
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

MAX_RETRIES = 5
RETRY_DELAY = 5  # Задержка в секундах между попытками


# Объявляем флаг для паузы
is_paused = False

# Функция для управления паузой
def check_for_pause():
    global is_paused
    while True:
        command = input().strip().lower()
        if command == 'pause':
            is_paused = True
            logging.info("Программа приостановлена.")
        elif command == 'resume':
            is_paused = False
            logging.info("Программа продолжает работу.")
        else:
            logging.info("Неверная команда. Используйте 'pause' или 'resume'.")

# Изменяем цикл скачивания сегментов
def download_segment(segment_url, segment_file, i, total_segments):
    attempts = 0
    while attempts < MAX_RETRIES:
        try:
            global is_paused
            # Ждем, пока пауза не будет снята
            while is_paused:
                time.sleep(1)  # Ожидаем, пока программа на паузе

            if os.path.exists(segment_file):
                logging.info(f"Сегмент {i + 1} из {total_segments}: уже скачан")
                return True

            logging.info(f"Скачиваем сегмент {i + 1} из {total_segments}: {segment_url}")
            response = requests.get(segment_url, timeout=10)
            response.raise_for_status()

            with open(segment_file, 'wb') as f:
                f.write(response.content)

            return True
        except requests.exceptions.RequestException as e:
            attempts += 1
            logging.warning(f"Ошибка при скачивании {segment_url}: {e}. Попытка {attempts} из {MAX_RETRIES}.")
            time.sleep(RETRY_DELAY)
        except KeyboardInterrupt:
            logging.info("Прерывание скачивания сегмента пользователем.")
            sys.exit(0)

    logging.error(f"Не удалось скачать сегмент {segment_url} после {MAX_RETRIES} попыток.")
    return False

def download_m3u8_video(playlist_url, output_dir):
    try:
        # Получаем уникальный идентификатор видео
        video_id = playlist_url.strip('/').split('/')[-3]
        logging.info(f"Идентификатор видео: {video_id}")

        # Создаем уникальную директорию для видео на основе идентификатора
        video_output_dir = os.path.join(output_dir, video_id)
        os.makedirs(video_output_dir, exist_ok=True)
        logging.info(f"Директория для видео: {video_output_dir}")

        segments_dir = os.path.join(video_output_dir, 'segments')
        os.makedirs(segments_dir, exist_ok=True)
        logging.info(f"Директория для сегментов: {segments_dir}")

        # Скачиваем плейлист
        response = requests.get(playlist_url, timeout=10)
        response.raise_for_status()

        m3u8_obj = m3u8.loads(response.text)
        total_segments = len(m3u8_obj.segments)

        for i, segment in enumerate(m3u8_obj.segments):
            segment_url = urljoin(playlist_url, segment.uri)
            segment_file = os.path.join(segments_dir, f"segment_{i}.ts")

            success = download_segment(segment_url, segment_file, i, total_segments)
            if not success:
                sys.exit(0)

        logging.info("Все сегменты скачаны.")

        # Проверяем, существует ли выходной файл
        output_video_path = os.path.join(video_output_dir, 'output.mp4')
        if os.path.exists(output_video_path):
            logging.info(f"Выходной файл {output_video_path} уже существует. Пропускаем объединение.")
            return

        # Создаем список файлов для объединения
        filelist_path = os.path.join(video_output_dir, 'filelist.txt')
        with open(filelist_path, 'w') as f:
            for i in range(total_segments):
                segment_path = os.path.join(segments_dir, f'segment_{i}.ts')
                if os.path.exists(segment_path):
                    f.write(f"file '{segment_path}'\n")
                else:
                    logging.warning(f"Сегмент {segment_path} отсутствует и будет пропущен при объединении.")

        # Вызываем FFmpeg для объединения сегментов
        ffmpeg_cmd = [
            'ffmpeg', '-f', 'concat', '-safe', '0', '-i', filelist_path,
            '-c', 'copy', output_video_path
        ]

        try:
            subprocess.run(ffmpeg_cmd, check=True)
            logging.info(f"Видео успешно собрано и сохранено в: {output_video_path}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Ошибка при объединении сегментов: {e}")

    except KeyboardInterrupt:
        logging.info("Прерывание выполнения программы пользователем.")
        sys.exit(0)

def download_mp4_video(video_url, output_dir):
    try:
        # Получаем уникальный идентификатор видео
        video_id = urlparse(video_url).path.split('/')[-1].split('.')[0]
        logging.info(f"Идентификатор видео: {video_id}")

        # Создаем уникальную директорию для видео на основе идентификатора
        video_output_dir = os.path.join(output_dir, video_id)
        os.makedirs(video_output_dir, exist_ok=True)

        video_path = os.path.join(video_output_dir, 'output.mp4')
        
        response = requests.get(video_url, stream=True)
        response.raise_for_status()

        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        logging.info(f"Видео успешно скачано и сохранено в: {video_path}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при скачивании видео: {e}")

if __name__ == "__main__":
    try:
        playlist_url = input("Введите ссылку на видео: ")
        output_dir = input("Введите путь к директории для сохранения видео: ")

        # Запуск потока для отслеживания паузы
        pause_thread = threading.Thread(target=check_for_pause)
        pause_thread.daemon = True
        pause_thread.start()

        _, file_extension = os.path.splitext(playlist_url)

        if file_extension == '.m3u8':
            download_m3u8_video(playlist_url, output_dir)
        elif file_extension == '.mp4':
            download_mp4_video(playlist_url, output_dir)
        else:
            print("Неподдерживаемый формат файла.")
    except KeyboardInterrupt:
        logging.info("Прерывание выполнения программы пользователем.")
        sys.exit(0)
"""