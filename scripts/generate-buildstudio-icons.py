from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "static" / "assets" / "buildstudio-there-emblem.png"
STATIC = ROOT / "static" / "static"
NAVY = (7, 16, 35, 255)


def fitted_logo(size: int, coverage: float = 0.9) -> Image.Image:
	logo = Image.open(SOURCE).convert("RGBA")
	bounds = logo.getbbox()
	if bounds:
		logo = logo.crop(bounds)

	target = max(1, round(size * coverage))
	logo.thumbnail((target, target), Image.Resampling.LANCZOS)
	canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
	canvas.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
	return canvas


def maskable_icon(size: int) -> Image.Image:
	canvas = Image.new("RGBA", (size, size), NAVY)
	logo = fitted_logo(size, 0.72)
	canvas.alpha_composite(logo)
	return canvas


def save_png(path: Path, size: int, coverage: float = 0.9) -> None:
	fitted_logo(size, coverage).save(path, format="PNG", optimize=True)


def main() -> None:
	STATIC.mkdir(parents=True, exist_ok=True)

	# Both /favicon.png and /static/favicon.png are used as fallbacks throughout the UI.
	save_png(ROOT / "static" / "favicon.png", 512)
	save_png(STATIC / "favicon.png", 512)
	save_png(STATIC / "favicon-96x96.png", 96)
	save_png(STATIC / "apple-touch-icon.png", 180, 0.78)
	save_png(STATIC / "logo.png", 512)
	save_png(STATIC / "splash.png", 512)
	save_png(STATIC / "splash-dark.png", 512)

	maskable_icon(192).save(STATIC / "web-app-manifest-192x192.png", format="PNG", optimize=True)
	maskable_icon(512).save(STATIC / "web-app-manifest-512x512.png", format="PNG", optimize=True)

	ico_source = fitted_logo(256, 0.9)
	ico_source.save(
		STATIC / "favicon.ico",
		format="ICO",
		sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
	)

	png_buffer = BytesIO()
	fitted_logo(512, 0.9).save(png_buffer, format="PNG", optimize=True)
	encoded = base64.b64encode(png_buffer.getvalue()).decode("ascii")
	(STATIC / "favicon.svg").write_text(
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
		f'<image width="512" height="512" href="data:image/png;base64,{encoded}"/>'
		"</svg>\n",
		encoding="utf-8",
	)


if __name__ == "__main__":
	main()
