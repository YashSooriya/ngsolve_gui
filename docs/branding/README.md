# LORENTZ brand assets

`Lorentz-Brand-Kit.zip` is the original artwork package supplied for the application rebrand. Its SVG masters, palette, and animation notes are the source of truth. The LORENTZ app uses optimized PNG/ICO exports from that kit:

| Asset | UI location |
|---|---|
| `lorentz_icon.ico` | Windows executable and native splash icon |
| `lorentz_mark_128x128.png` | Browser favicon |
| `lorentz_wordmark_light.png` / `lorentz_wordmark_dark.png` | Light/dark application header |
| `lorentz_home_light.png` | White new-project card |
| `lorentz_about_light.png` / `lorentz_about_dark.png` | Theme-matched About dialog banner |
| `lorentz_splash_dark_static_2x.png` and `lorentz_splash_dark_illumination_mask_2x.png` | Centered native splash and its text-only light sweep |
| `lorentz_splash_dark.png` | Static startup fallback if Pillow is unavailable |

The native splash remains still except for the restrained left-to-right highlight over the outlined wordmark. It uses the supplied 2× transparent layers, a 3.6-second cycle, 2.9 seconds of motion, and a brief pause. The background and letter shapes do not move. Internal Python package names, project formats, cache directories, and user-settings identifiers stay compatible with existing installations.
