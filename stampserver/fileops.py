import os


def unique_suffixed_name(res_dir, filename, suffix):
    """Return a filename (not a path) that inserts `suffix` before the extension
    of `filename` and is guaranteed not to collide with an existing file in
    `res_dir`.

    The `stamped.{tag}.` metadata prefix (if present) is preserved, and a
    numeric counter is appended when a name is already taken so repeated calls
    never collide:

        clip.mp4             -> clip_trimmed.mp4  (then _trimmed2, _trimmed3, ...)
        stamped.foo.clip.mp4 -> stamped.foo.clip_trimmed.mp4
    """
    base_name, extension = os.path.splitext(filename)

    stamp_prefix = ""
    actual_base = base_name
    if base_name.startswith("stamped."):
        parts = base_name.split(".", 2)  # ['stamped', '{tag}', 'basename']
        if len(parts) >= 3:
            stamp_prefix = f"stamped.{parts[1]}."
            actual_base = parts[2]
        elif len(parts) == 2:
            stamp_prefix = f"stamped.{parts[1]}."
            actual_base = ""

    counter = 1
    while True:
        tag = suffix if counter == 1 else f"{suffix}{counter}"
        new_filename = f"{stamp_prefix}{actual_base}{tag}{extension}"
        if not os.path.exists(os.path.join(res_dir, new_filename)):
            return new_filename
        counter += 1
