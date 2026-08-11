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

# .heic/.heif are offered even though ComfyUI cannot read them: cozy always
# stages those as PNG/JPEG (see fit.needs_transcode), so LoadImage never sees
# one, and the picker previews them transcoded.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif")

OUTPUT_SUFFIX = " [output]"


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
