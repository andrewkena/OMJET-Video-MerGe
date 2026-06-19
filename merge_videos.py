"""
Объединение видео-фрагментов вида:
    video_XXXXXXXX_XXXXXX_000.mp4
    video_XXXXXXXX_XXXXXX_001.mp4
    ...
в один файл:
    video_XXXXXXXX_XXXXXX.mp4

Версия 2: быстрое кодирование через ffmpeg + NVIDIA NVENC,
автозамена пропущенных фрагментов заглушкой "ФРАГМЕНТ УТЕРЯН".

Требования:
    - Установленный ffmpeg (и ffprobe), доступный в PATH.
      Скачать: https://www.gyan.dev/ffmpeg/builds/ (Windows, full build)
      Распаковать и добавить папку bin в переменную среды PATH,
      либо положить ffmpeg.exe/ffprobe.exe рядом со скриптом.
    - Видеокарта NVIDIA с поддержкой NVENC (есть почти на всех картах
      начиная с GeForce 600+). Если NVENC недоступен — скрипт сам
      переключится на обычное кодирование через CPU (libx264).

Исходные фрагменты после объединения НЕ удаляются.
"""

import os
import re
import sys
import json
import shutil
import subprocess
from collections import defaultdict
from typing import Optional

# Папка, где лежит exe (или скрипт при запуске через Python)
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Шаблон имени фрагмента: video_XXXXXXXX_XXXXXX_NNN.mp4
PART_PATTERN = re.compile(r"^(video_\d{8}_\d{6})_(\d{3})\.mp4$", re.IGNORECASE)

# Текст на заглушке для утерянных фрагментов
STUB_TEXT = "ФРАГМЕНТ УТЕРЯН"

# Шрифт для текста на заглушке. Если файл не найден по этому пути,
# скрипт попробует несколько стандартных вариантов для Windows/Linux.
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def find_ffmpeg_tool(name: str) -> str:
    """Находит ffmpeg/ffprobe: сначала в PATH, потом рядом со скриптом."""
    found = shutil.which(name)
    if found:
        return found
    local = os.path.join(BASE_DIR, f"{name}.exe" if os.name == "nt" else name)
    if os.path.exists(local):
        return local
    raise FileNotFoundError(
        f"Не найден '{name}'. Установите ffmpeg и добавьте его в PATH, "
        f"либо положите {name}.exe рядом со скриптом."
    )


FFMPEG = find_ffmpeg_tool("ffmpeg")
FFPROBE = find_ffmpeg_tool("ffprobe")


def find_font() -> Optional[str]:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def check_encoder_available(codec: str) -> bool:
    """Проверяет доступность кодека пробным мини-кодированием 1 кадра."""
    try:
        test_cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
            "-c:v", codec, "-f", "null", "-"
        ]
        result = subprocess.run(test_cmd, capture_output=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False


def find_best_encoder(codec_family: str) -> Optional[str]:
    """Возвращает первый доступный кодировщик для семейства 'hevc' или 'h264'.

    Порядок предпочтения: NVENC (NVIDIA) -> AMF (AMD) -> QSV (Intel) ->
    MF (Windows Media Foundation) -> программный (libx26x).
    """
    candidates = {
        "hevc": ["hevc_nvenc", "hevc_amf", "hevc_qsv", "hevc_mf", "libx265"],
        "h264": ["h264_nvenc", "h264_amf", "h264_qsv", "h264_mf", "libx264"],
    }
    for codec in candidates.get(codec_family, []):
        if check_encoder_available(codec):
            return codec
    return None


def probe_video(path: str) -> dict:
    """Возвращает параметры видео: длительность, разрешение, fps, кодеки, pixel format."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,sample_rate,channels",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    duration = float(data["format"]["duration"])
    width = height = None
    fps = "25/1"
    has_audio = False
    video_codec = "h264"
    pix_fmt = "yuv420p"
    audio_codec = "aac"
    audio_sample_rate = "44100"
    audio_channels = 2

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and width is None:
            width = stream.get("width")
            height = stream.get("height")
            fps = stream.get("r_frame_rate", "25/1")
            video_codec = stream.get("codec_name", "h264")
            pix_fmt = stream.get("pix_fmt", "yuv420p")
        elif stream.get("codec_type") == "audio":
            has_audio = True
            audio_codec = stream.get("codec_name", "aac")
            audio_sample_rate = stream.get("sample_rate", "44100")
            audio_channels = int(stream.get("channels", 2))

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": has_audio,
        "video_codec": video_codec,
        "pix_fmt": pix_fmt,
        "audio_codec": audio_codec,
        "audio_sample_rate": audio_sample_rate,
        "audio_channels": audio_channels,
    }


def _source_codec_family(reference_info: dict) -> str:
    """Возвращает 'hevc' или 'h264' по кодеку источника."""
    codec = reference_info["video_codec"].lower()
    if "hevc" in codec or "h265" in codec or "265" in codec:
        return "hevc"
    return "h264"


def get_encoder_video_args(encoder_name: str) -> list:
    """Возвращает ffmpeg-аргументы для заданного видеокодека."""
    if encoder_name in ("h264_nvenc", "hevc_nvenc"):
        return ["-c:v", encoder_name, "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    elif encoder_name in ("h264_amf", "hevc_amf"):
        return ["-c:v", encoder_name, "-quality", "balanced"]
    elif encoder_name in ("h264_qsv", "hevc_qsv"):
        return ["-c:v", encoder_name]
    elif encoder_name in ("h264_mf", "hevc_mf"):
        return ["-c:v", encoder_name]
    elif encoder_name == "libx264":
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"]
    elif encoder_name == "libx265":
        return ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "23"]
    else:
        return ["-c:v", encoder_name]


def get_stub_audio_codec_args(reference_info: dict) -> list:
    """Возвращает аргументы аудиокодека для заглушки, совпадающие с кодеком источника."""
    codec = reference_info["audio_codec"].lower()
    sample_rate = reference_info["audio_sample_rate"]
    channels = reference_info["audio_channels"]

    if "mp3" in codec or "mp2" in codec:
        return ["-c:a", "libmp3lame", "-b:a", "128k", "-ar", sample_rate, "-ac", str(channels)]
    elif "pcm" in codec or "wav" in codec:
        return ["-c:a", "pcm_s16le", "-ar", sample_rate, "-ac", str(channels)]
    else:
        return ["-c:a", "aac", "-b:a", "128k", "-ar", sample_rate, "-ac", str(channels)]


def create_stub_clip(output_path: str, reference_info: dict, stub_encoder: str) -> None:
    """Создаёт видео-заглушку с заданным кодеком и параметрами из reference_info."""
    width = reference_info["width"]
    height = reference_info["height"]
    fps = reference_info["fps"]
    duration = reference_info["duration"]
    pix_fmt = reference_info["pix_fmt"]
    sample_rate = reference_info["audio_sample_rate"]
    channels = reference_info["audio_channels"]
    channel_layout = "stereo" if channels >= 2 else "mono"

    font_path = find_font()

    # Windows paths like "C:/Windows/Fonts/arial.ttf" break ffmpeg's drawtext parser
    # because ":" is an option separator that cannot be reliably escaped in this build.
    # Fix: set cwd to the font directory so we can pass just the filename (no colon).
    if font_path:
        font_dir = os.path.dirname(font_path)
        font_filename = os.path.basename(font_path)
        font_arg = f":fontfile={font_filename}"
    else:
        font_dir = None
        font_arg = ""

    drawtext = (
        f"drawtext=text='{STUB_TEXT}'{font_arg}:"
        f"fontcolor=white:fontsize=h/15:"
        f"x=(w-text_w)/2:y=(h-text_h)/2"
    )

    video_args = get_encoder_video_args(stub_encoder)
    audio_args = get_stub_audio_codec_args(reference_info)

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={duration}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout={channel_layout}:sample_rate={sample_rate}",
        "-vf", drawtext,
        "-t", str(duration),
        "-pix_fmt", pix_fmt,
        *video_args,
        *audio_args,
        "-shortest",
        output_path,
    ]
    result = subprocess.run(cmd, cwd=font_dir, capture_output=True)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise subprocess.CalledProcessError(result.returncode, cmd[0], stderr=result.stderr)


def find_groups(folder: str) -> dict:
    """Группирует файлы-фрагменты по базовому имени."""
    groups = defaultdict(dict)
    for filename in os.listdir(folder):
        match = PART_PATTERN.match(filename)
        if not match:
            continue
        base_name, part_num = match.groups()
        groups[base_name][int(part_num)] = filename
    return groups


def merge_group(folder: str, base_name: str, parts: dict,
                hevc_encoder: Optional[str], h264_encoder: Optional[str]) -> bool:
    """Объединяет один набор фрагментов (с заменой пропусков заглушками)
    в итоговый файл base_name.mp4."""
    output_filename = f"{base_name}.mp4"
    output_path = os.path.join(folder, output_filename)

    if os.path.exists(output_path):
        print(f"  [ПРОПУЩЕНО] {output_filename} уже существует.")
        return False

    sorted_indices = sorted(parts.keys())
    full_range = list(range(sorted_indices[0], sorted_indices[-1] + 1))
    missing = sorted(set(full_range) - set(sorted_indices))

    reference_path = os.path.join(folder, parts[sorted_indices[0]])
    reference_info = probe_video(reference_path)

    source_family = _source_codec_family(reference_info)

    # Выбираем кодек для заглушки: предпочитаем совпадение с источником
    if source_family == "hevc":
        stub_encoder = hevc_encoder or h264_encoder
    else:
        stub_encoder = h264_encoder or hevc_encoder

    if stub_encoder is None:
        print(f"  [ОШИБКА] Не найден ни один доступный видеокодек для создания заглушки.")
        return False

    # Stream copy работает только если заглушки в том же кодеке, что и источник
    stub_family = "hevc" if ("hevc" in stub_encoder or "265" in stub_encoder) else "h264"
    can_stream_copy = (stub_family == source_family) or not missing

    if missing and not can_stream_copy:
        print(f"  [ПРЕДУПРЕЖДЕНИЕ] Кодировщик HEVC не найден. Заглушки будут H.264; "
              f"итоговый файл перекодируется в H.264 через {stub_encoder}.")

    temp_stub_files = []
    ordered_paths = []
    concat_list_path = os.path.join(folder, f"_concat_{base_name}.txt")

    try:
        for idx in full_range:
            if idx in parts:
                ordered_paths.append(os.path.join(folder, parts[idx]))
            else:
                stub_name = f"_stub_{base_name}_{idx:03d}.mp4"
                stub_path = os.path.join(folder, stub_name)
                print(f"  [ЗАГЛУШКА] Часть {idx:03d} отсутствует -> создаю '{stub_name}' "
                      f"({reference_info['duration']:.2f} сек, кодек: {stub_encoder})")
                create_stub_clip(stub_path, reference_info, stub_encoder)
                ordered_paths.append(stub_path)
                temp_stub_files.append(stub_path)

        print(f"  Всего частей в итоговом видео: {len(ordered_paths)} "
              f"(из них заглушек: {len(missing)})")
        for p in ordered_paths:
            print(f"    - {os.path.basename(p)}")

        with open(concat_list_path, "w", encoding="utf-8") as f:
            for p in ordered_paths:
                escaped = p.replace("\\", "/").replace("'", "\\'")
                f.write(f"file '{escaped}'\n")

        if can_stream_copy:
            codec_cmd = ["-c", "copy"]
            print(f"  Склейка (stream copy, без перекодирования) -> {output_filename} ...")
        else:
            codec_cmd = [
                *get_encoder_video_args(stub_encoder),
                "-pix_fmt", "yuv420p",
                *get_stub_audio_codec_args(reference_info),
            ]
            print(f"  Склейка (перекодирование через {stub_encoder}) -> {output_filename} ...")

        cmd = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            *codec_cmd,
            output_path,
        ]
        subprocess.run(cmd, check=True)

        print(f"  [ГОТОВО] {output_filename}")
        return True

    except subprocess.CalledProcessError as e:
        stderr_text = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        if stderr_text:
            print(f"    ffmpeg stderr:\n{stderr_text}")
        print(f"  [ОШИБКА] ffmpeg завершился с ошибкой при обработке '{base_name}': {e}")
        return False
    except Exception as e:
        print(f"  [ОШИБКА] Не удалось объединить '{base_name}': {e}")
        return False
    finally:
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)
        for stub in temp_stub_files:
            if os.path.exists(stub):
                os.remove(stub)


def main():
    print(f"Папка поиска: {BASE_DIR}")
    print("Определение доступных видеокодеков...")

    hevc_encoder = find_best_encoder("hevc")
    h264_encoder = find_best_encoder("h264")

    print(f"  HEVC: {hevc_encoder or 'нет (заглушки HEVC будут H.264, итог перекодируется)'}")
    print(f"  H.264: {h264_encoder or 'нет'}")
    print()

    if hevc_encoder is None and h264_encoder is None:
        print("[КРИТИЧЕСКАЯ ОШИБКА] Не найден ни один видеокодек. "
              "Установите ffmpeg с libx264/libx265 или обновите драйверы GPU.")
        return

    groups = find_groups(BASE_DIR)

    if not groups:
        print("Не найдено файлов вида video_XXXXXXXX_XXXXXX_NNN.mp4 в этой папке.")
        return

    print(f"Найдено наборов для объединения: {len(groups)}\n")

    success_count = 0
    for base_name in sorted(groups.keys()):
        print(f"Обработка: {base_name}")
        if merge_group(BASE_DIR, base_name, groups[base_name], hevc_encoder, h264_encoder):
            success_count += 1
        print()

    print(f"Готово. Успешно объединено наборов: {success_count} из {len(groups)}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[КРИТИЧЕСКАЯ ОШИБКА] {e}")
    finally:
        if getattr(sys, "frozen", False):
            input("\nНажмите Enter для выхода...")
