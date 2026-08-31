"""Map picker values to on-disk image paths.

cozy offers input-dir and output-dir images in one dropdown. The option `value`
is exactly the string handed to ComfyUI's LoadImage `image` input: a bare
relative path for input-dir files, suffixed with ' [output]' for output-dir
files (folder_paths.annotated_filepath resolves that suffix against ComfyUI's
output directory), so the same string is the LoadImage input, the persisted
selection, and the preview key -- no conversion anywhere. Offering output-dir
files is what lets a prior generation be re-fed as the next edit's input.

Lives apart from cozy.py because both the Flask app and the queue Scheduler
need to resolve a picker value to a real file.
"""
import os
import re

# What ComfyUI's LoadImage can read. The input/output dropdown and resolve()
# are limited to these because those files reach LoadImage untouched.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")

# HEIF is what phones shoot, so the remote (wormhole) picker offers it even
# though ComfyUI cannot read it: images arriving that way are transcoded to
# PNG as they are staged into the input dir, so LoadImage never sees a HEIF.
# A HEIF sitting in ComfyUI's own input dir is still not offered -- nothing
# would transcode it.
HEIF_EXTS = (".heic", ".heif")
PICKABLE_EXTS = IMAGE_EXTS + HEIF_EXTS

OUTPUT_SUFFIX = " [output]"

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_upload_name(filename):
    """Reduce a browser-supplied filename to a bare safe basename, or None.

    None means the extension is not one the picker accepts. The result is both
    a path under the input dir and part of the LoadImage string ComfyUI gets,
    so it has to be a single plain segment: browsers send directory components
    on some platforms, and spaces, quotes or unicode on others. The extension
    is preserved as-is (lower-cased) because callers key transcoding off it.
    """
    base = os.path.basename(filename.replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in PICKABLE_EXTS:
        return None
    # Trim leading dots so an upload can never become a hidden file, and cap
    # the length well short of any filesystem limit once suffixes are added.
    stem = _UNSAFE_NAME_CHARS.sub("_", stem).strip("._-")[:100]
    return (stem or "upload") + ext


def list_dir_images(directory):
    """Sorted relative paths of image files under directory (empty if unset)."""
    out = []
    if not directory:
        return out
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith(IMAGE_EXTS):
                out.append(os.path.relpath(os.path.join(root, f), directory))
    return sorted(out)


def list_images(input_dir, output_dir):
    """Picker options spanning the input and output dirs. `label` is the bare
    path for display; `source` groups the two in the UI."""
    items = [{"value": r, "label": r, "source": "input"}
             for r in list_dir_images(input_dir)]
    items += [{"value": r + OUTPUT_SUFFIX, "label": r, "source": "output"}
              for r in list_dir_images(output_dir)]
    return items


def resolve(input_dir, output_dir, value):
    """Map a picker value to an on-disk path, or None if it is not a valid image
    within the directory it names. Output-dir files carry the ' [output]'
    annotation; everything else resolves under the input dir. Rejects traversal
    out of the chosen base via realpath containment."""
    if not value:
        return None
    if value.endswith(OUTPUT_SUFFIX):
        base, rel = output_dir, value[:-len(OUTPUT_SUFFIX)]
    else:
        base, rel = input_dir, value
    if not base or not rel.lower().endswith(IMAGE_EXTS):
        return None
    full = os.path.realpath(os.path.join(base, rel))
    root = os.path.realpath(base)
    if os.path.commonpath([full, root]) != root or not os.path.isfile(full):
        return None
    return full
