import os
import json
import tempfile
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

# Define your default schema here. Add any future settings keys to this dictionary.
DEFAULT_SETTINGS = {
    "last_folder": "",
    "keep_detached_viewers_alive": False,
    "favorites": [],
    "max_favorites": 15,
    "sort_criteria": "name",
    "sort_reverse": False,
    "theme_style": "Cool Blue-Slate (Default)",
    "language": "English (US)",
    "prefetch_images": True,
    "smooth_scaling": True,
    "purge_thumb_cache_on_exit": False,
    "encrypt_thumbnail_cache": True,
    "max_thumbnail_cache": "Auto (System RAM)",
    "thumbnail_jpeg_quality": 88,
    "disk_cache_limit_mb": "Disabled",
    "thread_pool_concurrency": "Auto (Max Cores)",
    "confirm_on_delete": True,
    "loop_gallery_navigation": True,
    "include_subfolders": False,
    "subfolder_depth": 1,
    "scroll_speed": 15,
    "default_zoom_mode": "Fit to Window (Default)",
    "invert_mouse_wheel_zoom": False,
    "mouse_zoom": True,
    "viewer_always_on_top": True
}


def load_settings(settings_file="settings.json"):
    """Loads settings from JSON, merging with defaults so new keys are always present."""
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                if isinstance(user_data, dict):
                    settings.update(user_data)
        except Exception as e:
            print(f"Error loading settings: {e}")
    return settings


def save_settings(settings_file="settings.json", new_values=None):
    """Safely updates and persists settings to JSON atomically without losing other keys."""
    current_settings = load_settings(settings_file)

    if new_values and isinstance(new_values, dict):
        current_settings.update(new_values)

    try:
        abs_settings = os.path.abspath(settings_file)
        target_dir = os.path.dirname(abs_settings) or "."
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=target_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(current_settings, f, indent=4)
            os.replace(tmp_path, abs_settings)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    except Exception as e:
        print(f"Error saving settings: {e}")


def format_file_size(b_size):
    """Converts raw byte size into a clean human-readable KB or MB string."""
    if b_size >= 1024 * 1024:
        return f"{b_size / (1024 * 1024):.2f} MB"
    else:
        return f"{b_size / 1024:.1f} KB"


def extract_exif_metadata(file_path):
    """Extracts and formats comprehensive EXIF metadata and image info headers."""
    exif_text = "<b>EXIF Camera Data:</b><br><br>"
    found_exif = False
    try:
        with Image.open(file_path) as img:
            raw_exif = None
            try:
                raw_exif = img.getexif()
            except Exception:
                pass
            if not raw_exif and hasattr(img, "_getexif"):
                try:
                    raw_exif = img._getexif()
                except Exception:
                    pass

            exif_dict = {}

            if raw_exif:
                for tag_id, val in raw_exif.items():
                    tag_name = TAGS.get(tag_id, tag_id)
                    if isinstance(val, bytes):
                        try:
                            val = val.decode('utf-8', errors='ignore').strip()
                        except Exception:
                            val = str(val)
                    elif isinstance(val, tuple):
                        val = ", ".join(str(v) for v in val)
                    exif_dict[str(tag_name)] = val

            if hasattr(img, 'info') and img.info:
                for k, v in img.info.items():
                    if k not in ['exif', 'icc_profile'] and isinstance(v, (str, int, float)):
                        exif_dict[str(k)] = v

            if exif_dict:
                exif_lines = []
                priority_keys = [
                    "Make", "Model", "LensModel", "DateTimeOriginal",
                    "ExposureTime", "FNumber", "ISOSpeedRatings",
                    "FocalLength", "ExposureBiasValue", "Flash"
                ]

                for k in priority_keys:
                    if k in exif_dict:
                        exif_lines.append(f"<b>{k}:</b> {exif_dict[k]}")
                        del exif_dict[k]

                for k, v in sorted(exif_dict.items()):
                    if len(str(v)) < 150:
                        exif_lines.append(f"<b>{k}:</b> {v}")

                if exif_lines:
                    exif_text += "<br>".join(exif_lines)
                    found_exif = True
    except Exception as e:
        exif_text += f"<i>Error reading EXIF: {str(e)}</i>"

    if not found_exif:
        exif_text = "<i>No EXIF metadata found for this image (common in web PNGs or screenshots).</i>"

    return exif_text


def extract_image_metadata(path):
    """
    Extracts raw binary EXIF bytes and a complete human-readable metadata dictionary
    from WebP, JPEG, and PNG files, handling raw byte stream reloads for WebP.
    Returns: (exif_data_bytes, exif_dict)
    """
    exif_data_bytes = None
    exif_dict = {}

    EXIF_IFD_TAG = 34665
    GPS_IFD_TAG = 34853
    INTEROP_IFD_TAG = 40965

    try:
        with Image.open(path) as src:
            exif_data_bytes = src.info.get('exif')

            loaded_exif = None
            if exif_data_bytes:
                try:
                    loaded_exif = Image.Exif()
                    loaded_exif.load(exif_data_bytes)
                except Exception:
                    pass

            if not loaded_exif:
                try:
                    loaded_exif = src.getexif()
                except Exception:
                    pass

            if loaded_exif:
                if not exif_data_bytes:
                    try:
                        exif_data_bytes = loaded_exif.tobytes()
                    except Exception:
                        pass

                for tag_id, val in loaded_exif.items():
                    tag_name = TAGS.get(tag_id, tag_id)
                    exif_dict[str(tag_name)] = _clean_metadata_value(val)

                for ifd_tag, tag_lookup in [(EXIF_IFD_TAG, TAGS), (GPS_IFD_TAG, GPSTAGS), (INTEROP_IFD_TAG, TAGS)]:
                    try:
                        sub_ifd = loaded_exif.get_ifd(ifd_tag)
                        for sub_id, sub_val in sub_ifd.items():
                            sub_name = tag_lookup.get(sub_id, sub_id)
                            exif_dict[str(sub_name)] = _clean_metadata_value(sub_val)
                    except Exception:
                        pass

            if hasattr(src, "_getexif"):
                try:
                    raw_exif = src._getexif()
                    if raw_exif:
                        for k, v in raw_exif.items():
                            tag_name = TAGS.get(k, k)
                            exif_dict[str(tag_name)] = _clean_metadata_value(v)
                except Exception:
                    pass

            if hasattr(src, 'info') and src.info:
                for k, v in src.info.items():
                    if k not in ['exif', 'icc_profile', 'jfif'] and isinstance(v, (str, int, float, bytes)):
                        exif_dict[str(k)] = _clean_metadata_value(v)

    except Exception as e:
        print(f"Error extracting metadata from {path}: {e}")

    filtered_exif_dict = {
        k: v for k, v in exif_dict.items()
        if k and not k.lower().startswith("jfif") and not k.isdigit() and str(v).strip()
    }

    return exif_data_bytes, filtered_exif_dict


def _clean_metadata_value(val):
    if isinstance(val, bytes):
        try:
            return val.decode('utf-8', errors='ignore').strip()
        except Exception:
            return str(val)
    elif isinstance(val, tuple):
        return ", ".join(str(item) for item in val)
    return str(val)


def add_folder_to_favorites(folder_path, settings_file="settings.json"):
    """Adds a folder to favorites, moving it to the front if it exists, and caps by max_favorites."""
    settings = load_settings(settings_file)
    favorites = settings.get("favorites", [])
    try:
        max_favs = int(settings.get("max_favorites", 15))
    except (TypeError, ValueError):
        max_favs = 15
    max_favs = max(1, max_favs)

    norm_path = os.path.normpath(folder_path)
    favorites = [p for p in favorites if os.path.normpath(p) != norm_path]
    favorites.insert(0, folder_path)
    favorites = favorites[:max_favs]

    save_settings(settings_file, {"favorites": favorites})
    return favorites


def extract_comfyui_workflow(path):
    """Extracts raw ComfyUI workflow or prompt metadata from PNG info chunks if present."""
    try:
        with Image.open(path) as img:
            if img.format == "PNG" and hasattr(img, "text") and img.text:
                for key in ["workflow", "prompt"]:
                    if key in img.text:
                        try:
                            parsed = json.loads(img.text[key])
                            return json.dumps(parsed, indent=4)
                        except Exception:
                            return img.text[key]
    except Exception as e:
        print(f"Error reading ComfyUI metadata: {e}")
    return None


def get_optimal_thumbnail_cache_limit():
    """Calculates optimal RAM cache item count based on total system memory."""
    try:
        import psutil
        total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    except ImportError:
        total_ram_gb = 16.0

    if total_ram_gb <= 8.0:
        return 2000    # ~225 MB for 8GB RAM systems
    elif total_ram_gb <= 16.0:
        return 5000    # ~560 MB for 16GB RAM systems
    elif total_ram_gb <= 32.0:
        return 10000   # ~1.1 GB for 32GB RAM systems
    else:
        return 20000   # ~2.2 GB for high-end workstations (64GB+)