# endcord-image-inline
An extension for [endcord](https://github.com/sparklost/endcord) discord TUI client, that adds drawing inline images in the chat using kitty protocol.  
Should work on all terminals with kitty image protocol support.  
If running on endcord-lite (without media support) it will use significantly more bandwidth because it is downloading unresized png images.  
When installed it will replace existing inline image drawing (only if kitty-protocol is supported by current terminal).  
If running in tmux, don't forget to set `allow-passthrough`.  
If there are issues with terminals that have kitty protocol, other than kitty itself, its **their problem**.  

## Installing
See [official extensions documentation](https://github.com/sparklost/endcord/blob/main/extensions.md#installing-extensions) for installing instructions.
Available options:
- Git clone into `Extensions` directory located in endcord config directory.
- Run `endcord -i https://github.com/sparklost/endcord-image-inline`
- Or use endcord client-side command `install_extension sparklost/endcord-image-inline`


# Configuration
This extension is using already existing endcord options:
- `inline_media = True`  
    Toggle this extension ON/OFF.
- `inline_media_height`  
    Height of DISPLAYED image in chat. In number of characters.
- `inline_media_download_size = 256`  
    Height of DOWNLOADED image thumb for chat inline media. In pixels.
- `max_thumb_cache_age = 14`  
    How long to keep cached thumbs for inline images, in days. Set `0` to clear on each new run.
- `media_font_aspect_ratio = None`  
    Aspect ratio of character height / width. It is automatically detected, but if its wrong, set it here to float value eg `2.25`.

## Disclaimer
This extension is usable only in bots which should not be breaking any ToS, byt here's a warning anyway:  
> [!WARNING]
> Using third-party client is against Discord's Terms of Service and may cause your account to be banned!  
> **Use endcord and/or this extension at your own risk!**  
> If this extension is modified, it may be used for harmful or unintended purposes.  
> **The developer is not responsible for any misuse or for actions taken by users.**  
