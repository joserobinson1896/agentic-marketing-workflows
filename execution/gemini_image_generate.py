"""
Layer 3 execution tool: generate/edit images with Gemini's image model
(the "nano-banana" family) instead of HTML/CSS compositing.

Steering pattern (mirrors how ad_creator.py steers HTML compositing, but for
a generative image model):
  - `prompt` carries the art direction (layout, copy placement, mood, brand
    colors) the same way the HTML template used to.
  - `reference_image_paths` are extra input images the model conditions on:
    the product photo (so it renders that exact product) plus 1+ style
    reference images (so it matches composition/lighting/mood). The model
    receives them as ordinary Content parts alongside the text prompt.

CLI usage:
    python execution/gemini_image_generate.py \
        --prompt "..." \
        --image path/to/product.png \
        --image path/to/style_ref.jpg \
        --out .tmp/gemini_images/out.png \
        --aspect-ratio 4:5

Importable:
    from gemini_image_generate import generate_image
    generate_image(prompt="...", reference_image_paths=["product.png"], output_path="out.png")
"""

import argparse
import os

from dotenv import load_dotenv

# Confirmed against the installed google-genai SDK source (2.22.0): the
# client.models.generate_content(...) path (not the newer client.interactions
# path some docs now front) is what's stable/well-documented for image output
# via response_modalities=["TEXT", "IMAGE"] + GenerateContentConfig.image_config.
DEFAULT_MODEL = "gemini-2.5-flash-image"

# Newer model IDs surfaced in current Gemini docs (Sept 2026) — untested here,
# pass --model to try one:
#   gemini-3.1-flash-image       (versatile, 512px-4K)
#   gemini-3.1-flash-lite-image  (fastest/cheapest, 1K only)
#   gemini-3-pro-image           (premium, most reference images / complex scenes)


def _get_client():
    # Explicit path, not a cwd-relative search: under launchd there is no shell
    # environment and the working directory can't be assumed, so a bare
    # load_dotenv() would silently find nothing and fail at auth time instead.
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    from google import genai

    return genai.Client(api_key=api_key)


def generate_image(
    prompt: str,
    reference_image_paths=None,
    output_path: str = ".tmp/gemini_images/output.png",
    aspect_ratio: str = "4:5",
    image_size: str = "2K",
    model: str = DEFAULT_MODEL,
):
    """Generate (or edit/compose) an image with Gemini.

    Returns dict: {"output_path": str, "text": str|None, "model": str}
    Raises RuntimeError if the model returned no image (e.g. safety block).
    """
    from google.genai import types
    from PIL import Image

    client = _get_client()

    contents = [prompt]
    for path in reference_image_paths or []:
        contents.append(Image.open(path))

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            ),
        ),
    )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    saved_path = None
    text_parts = []
    candidates = response.candidates or []
    if not candidates:
        feedback = getattr(response, "prompt_feedback", None)
        raise RuntimeError(f"No candidates returned. prompt_feedback={feedback}")

    for part in candidates[0].content.parts:
        if getattr(part, "text", None):
            text_parts.append(part.text)
        elif getattr(part, "inline_data", None) is not None:
            image = part.as_image()
            image.save(output_path)
            saved_path = output_path

    if saved_path is None:
        finish_reason = getattr(candidates[0], "finish_reason", None)
        raise RuntimeError(
            f"Model returned no image (finish_reason={finish_reason}). "
            f"Text response: {' '.join(text_parts) or '(none)'}"
        )

    return {"output_path": saved_path, "text": " ".join(text_parts) or None, "model": model}


def main():
    parser = argparse.ArgumentParser(description="Generate/edit an image with Gemini")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--image", action="append", dest="images", default=[], help="Reference image path (repeatable)")
    parser.add_argument("--out", default=".tmp/gemini_images/output.png")
    parser.add_argument("--aspect-ratio", default="4:5")
    parser.add_argument("--image-size", default="2K")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    result = generate_image(
        prompt=args.prompt,
        reference_image_paths=args.images,
        output_path=args.out,
        aspect_ratio=args.aspect_ratio,
        image_size=args.image_size,
        model=args.model,
    )
    print(f"MODEL={result['model']}")
    print(f"OUTPUT_PATH={result['output_path']}")
    if result["text"]:
        print(f"MODEL_TEXT={result['text']}")


if __name__ == "__main__":
    main()
