import base64
import glob
import importlib
import logging
import os
import queue
import sys
import threading

from endcord import peripherals, terminal_utils, utils

EXT_NAME = "Image Inline"
EXT_VERSION = "0.1.3"
EXT_ENDCORD_VERSION = "1.5.0"
EXT_DESCRIPTION = "An extension that adds drawing inline images in the chat using kitty protocol"
EXT_SOURCE = "https://github.com/sparklost/endcord-image-inline"
logger = logging.getLogger(__name__)
support_media = importlib.util.find_spec("PIL") is not None

START_IMAGE_ID = 5000


def check_kitty():
    """Check if kitty protocol is supported"""
    response = terminal_utils.query_terminal(b"\x1b_Gi=1,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c")
    return "OK" in response


def kitty_upload_png(path, image_id):
    """Upload base64 encoded png into kitty image cache"""
    with open(path, "rb") as f:
        png_data = f.read()
    payload = base64.b64encode(png_data).decode("ascii")
    for i in range(0, len(payload), 4096):
        chunk = payload[i:i + 4096]
        more = 1 if i + 4096 < len(payload) else 0
        if i == 0:
            header = f"a=t,f=100,q=2,i={image_id},m={more}"
        else:
            header = f"m={more}"
        os.write(sys.stdout.fileno(), f"\033_G{header};{chunk}\033\\".encode())
    return True


def kitty_upload_image(path, image_id):
    """Upload base64 encoded any image into kitty image cache"""
    from PIL import Image
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        return False
    w_px, h_px = img.size
    payload = base64.b64encode(img.tobytes()).decode("ascii")
    for i in range(0, len(payload), 4096):
        chunk = payload[i:i + 4096]
        more = 1 if i + 4096 < len(payload) else 0
        if i == 0:
            header = f"a=t,f=32,q=2,s={w_px},v={h_px},i={image_id},m={more}"
        else:
            header = f"m={more}"
        os.write(sys.stdout.fileno(), f"\033_G{header};{chunk}\033\\".encode())
    return True


def kitty_draw_image_by_id(image_id, x, y, w=None, h=None, cut_y=None, cut_h=None):
    """Draw previously uploaded image by its id"""
    header = f"a=p,q=2,z=-1,i={image_id}"
    if w is not None:
        header += f",c={w}"
    if h is not None:
        header += f",r={h}"
    if cut_y is not None:
        header += f",y={cut_y}"
    if cut_h is not None:
        header += f",h={cut_h}"
    # \0337 is remember cursor, \0338 is restore cursor, \033[y,x]H is move cursor
    os.write(sys.stdout.fileno(), f"\0337\033[{y+1};{x+1}H\033_G{header}\033\\\0338".encode())


def kitty_delete_images_by_id(image_id):
    """Delete all images with this id and remove it from memory"""
    os.write(sys.stdout.fileno(), f"\033_Ga=d,d=I,q=2,i={image_id}\033\\".encode())


def kitty_clear_images_by_id(image_id):
    """Delete all images with this id but keep image in memory"""
    os.write(sys.stdout.fileno(), f"\033_Ga=d,d=i,q=2,i={image_id}\033\\".encode())


class Extension:
    """Main extension class"""

    def __init__(self, app):
        self.app = app
        self.tui = app.tui
        self.run = True
        kitty_supported = getattr(self.tui, "kitty_supported", None)
        if kitty_supported is False or (kitty_supported is not True and not check_kitty()):
            logger.warning("No kitty protocol support detected in this terminal")
            self.run = False
        if not self.app.config["inline_media"]:
            self.run = False
        self.cell_w, self.cell_h = terminal_utils.get_font_size()
        if not self.cell_w:
            self.run = False

        if not self.run:
            del type(self).on_chat_update
            del type(self).on_chat_draw
            self.tui.kitty_supported = False
            return

        self.app.placeholder_images = True
        self.app.formatter.placeholder_images = self.app.config["inline_media_height"]
        self.app.inline_media = False
        self.inline_media_quality = self.app.config["inline_media_quality"]
        if self.inline_media_quality not in ("lossless", "low", "high"):
            self.inline_media_quality = "low"

        self.chat_map = []
        self.update = threading.Event()
        self.drawing = threading.Event()
        self.prev_chat_index = None
        self.prev_chat_hw = None
        self.prew_win_hw = self.tui.screen_hw
        self.force_draw = False
        self.image_cache_path = os.path.expanduser(os.path.join(peripherals.cache_path, "images"))
        self.image_cache = {}
        self.download_queue = queue.Queue()
        self.image_cache_lock = threading.Lock()

        threading.Thread(target=utils.delete_old_files, daemon=True, args=(
            os.path.join(peripherals.cache_path, "images"),
            self.app.config["max_thumb_cache_age"],
            True,
        )).start()
        threading.Thread(target=self.downloader, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()


    def on_chat_update(self, chat, chat_format, chat_map):   # noqa
        """Get new chat map"""
        self.chat_map = chat_map
        self.update.set()


    def on_chat_draw(self):
        """Re-calculate image positions and draw them"""
        if not self.force_draw and self.prev_chat_index == self.tui.chat_index and self.prev_chat_hw == self.tui.chat_hw:
            return
        if self.prew_win_hw != self.tui.screen_hw:
            self.prew_win_hw = self.tui.screen_hw
            self.reupload_all()
        with self.tui.lock:
            chat_y, chat_x = self.tui.win_chat.getbegyx()
            chat_h = self.tui.chat_hw[0]
            with self.image_cache_lock:
                for kitty_image_id, rel_y, rel_x, h, w, img_scale in self.image_cache.values():
                    kitty_clear_images_by_id(kitty_image_id)
                    abs_y = chat_h - (rel_y - self.tui.chat_index - self.tui.have_title + 1)
                    if abs_y - chat_y <= -h or abs_y >= chat_h:
                        continue
                    abs_x = chat_x + rel_x
                    cut_y = None
                    cut_h = None
                    h_1 = h
                    if abs_y > chat_h - h + 1:
                        h_1 = min(h, chat_h - abs_y + 1)
                        cut_h = int(h_1 * self.cell_h * img_scale)
                    if abs_y <= 0:
                        h_1 += abs_y - chat_y
                        cut_y = int(((-abs_y * self.cell_h) + self.cell_h) * img_scale)
                        abs_y = chat_y
                    # logger.info((kitty_image_id, abs_y, rel_y, h_1, img_scale, cut_y, cut_h))
                    kitty_draw_image_by_id(kitty_image_id, x=abs_x, y=abs_y, w=w, h=h_1, cut_y=cut_y, cut_h=cut_h)
        self.prev_chat_index = self.tui.chat_index
        self.prev_chat_hw = self.tui.chat_hw


    def on_force_redraw(self):
        """When curses screen.clear(), kitty images are cleared too so redraw them"""
        self.reupload_all()


    def reupload_all(self):
        """Delete all images and trigger reupload"""
        for image in self.image_cache.values():
            with self.tui.lock:
                kitty_clear_images_by_id(image[0])
                kitty_delete_images_by_id(image[0])
        self.image_cache = {}
        self.update.set()


    def get_free_id(self, new_cache):
        """Get first free id"""
        ids = sorted(set(i[0] for i in self.image_cache.values()) | set(i[0] for i in new_cache.values()))
        for i in range(len(ids) - 1):
            if ids[i + 1] != ids[i] + 1:
                return ids[i] + 1
        if START_IMAGE_ID not in ids:
            return START_IMAGE_ID
        return START_IMAGE_ID + len(ids)


    def worker(self):
        """Thread that updates image cache on disk and in ram and downloads missing images"""
        while self.run:
            self.update.wait()
            self.update.clear()
            image_cache = {}
            self.force_draw = False

            for rel_y, line_map in enumerate(self.chat_map):
                if not line_map:
                    continue
                if not line_map[5]:
                    continue
                img_pos = line_map[5][5]
                if not img_pos or len(img_pos) < 4:
                    continue
                rel_x, w, embed_idx, h, = img_pos

                # get message and image info
                try:
                    message = self.app.messages[line_map[0]]
                    message_id = message["id"]
                    embed = message["embeds"][embed_idx]
                    img_url = embed["proxy_url"]
                    img_h, img_w = embed["hw"]
                    scale = min(h * self.cell_h / img_h, w * self.cell_w / img_w, 1)
                    img_w = round(img_w * scale)
                    img_h = round(img_h * scale)
                    img_scale = 1
                    image_id = f"{message_id}_{embed_idx}"
                except IndexError:
                    continue

                # download and cache (disk and ram)
                if image_id not in self.image_cache:
                    img_format = "webp" if support_media else "png"
                    img_quality = "lossless" if support_media and "//media." in img_url else self.inline_media_quality
                    if img_url.endswith("&"):
                        img_url += "="
                    if "?" not in img_url:
                        img_url += "?"

                    # reuse larger cached image or delete smaller
                    for path in glob.glob(os.path.join(self.image_cache_path, f"{image_id}_*")):
                        try:
                            filename = os.path.splitext(os.path.basename(path))[0]
                            _, _, new_w, new_h = filename.split(".")[0].split("_")
                            if int(new_w) >= img_w:
                                img_w = int(new_w)
                                img_scale = int(new_h) / img_h
                                img_h = int(new_h)
                                break
                            else:
                                os.remove(path)
                                break   # assuming only one can exist
                        except Exception:
                            pass

                    # download and draw
                    img_url = f"{img_url}&format={img_format}&quality={img_quality}&width={img_w}&height={img_h}"
                    img_name = f"{image_id}_{img_w}_{img_h}.{img_format}"
                    kitty_image_id = self.get_free_id(image_cache)
                    self.download_queue.put((img_url, img_name, image_id, kitty_image_id, rel_y, rel_x, h, w, img_scale))
                    image_cache[image_id] = (kitty_image_id, rel_y, rel_x, h, w, img_scale)
                else:
                    image_cache[image_id] = (self.image_cache[image_id][0], rel_y, rel_x, h, w, self.image_cache[image_id][5])

            # update cahanged images and delete unused cache
            if image_cache != self.image_cache:
                deleted_kitty = []
                with self.image_cache_lock:
                    for image_id in self.image_cache:
                        if image_id not in image_cache:
                            deleted_kitty.append(self.image_cache[image_id][0])
                    self.image_cache = image_cache
                with self.tui.lock:
                    for kitty_image_id in deleted_kitty:
                        kitty_clear_images_by_id(kitty_image_id)
                        kitty_delete_images_by_id(kitty_image_id)
                self.force_draw = True
                self.on_chat_draw()


    def downloader(self):
        """Download image and draw it"""
        while self.run:
            img_url, img_name, image_id, kitty_image_id, rel_y, rel_x, h, w, img_scale = self.download_queue.get()
            image_path = self.app.discord.get_file(img_url, self.image_cache_path, file_name=img_name, cache=True)
            if not image_path:
                continue
            with self.tui.lock:
                if support_media:
                    success = kitty_upload_image(image_path, kitty_image_id)
                else:
                    success = kitty_upload_png(image_path, kitty_image_id)
            if not success:
                continue
            if image_id not in self.image_cache:
                continue

            chat_y, chat_x = self.tui.win_chat.getbegyx()
            chat_h = self.tui.chat_hw[0]
            with self.tui.lock:
                with self.image_cache_lock:
                    abs_y = chat_h - (rel_y - self.tui.chat_index - self.tui.have_title + 1)
                    if abs_y - chat_y <= -h or abs_y >= chat_h:
                        continue
                    abs_x = chat_x + rel_x
                    cut_y = None
                    cut_h = None
                    h_1 = h
                    if abs_y > chat_h - h + 1:
                        h_1 = min(h, chat_h - abs_y + 1)
                        cut_h = int(h_1 * self.cell_h * img_scale)
                    if abs_y <= 0:
                        h_1 += abs_y - chat_y
                        cut_y = int(((-abs_y * self.cell_h) + self.cell_h) * img_scale)
                        abs_y = chat_y
                    # logger.info((kitty_image_id, abs_y, rel_y, h_1, img_scale, cut_y, cut_h))
                    kitty_draw_image_by_id(kitty_image_id, x=abs_x, y=abs_y, w=w, h=h_1, cut_y=cut_y, cut_h=cut_h)
