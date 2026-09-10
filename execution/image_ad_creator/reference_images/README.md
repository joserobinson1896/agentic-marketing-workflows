# Reference images

**These files are not in the repo. You supply them.** The batch specs point here by exact
filename, so dropping in files with the names below makes the pipeline run as documented.

They live beside the scripts rather than in `.tmp/` on purpose: the unattended daily job
depends on them, and `.tmp/` is disposable and gitignored. A daily job cannot depend on
disposable inputs.

## What to put here

| Filename | What it is |
|---|---|
| `product_ai_sneaker.jpeg` | **The product photo.** The fidelity anchor — the model reproduces this exact product in every generated scene. A clean, well-lit shot of the real product on a plain background works best |
| `ref_white_platform_wood.png` | A style reference: bright studio, product on a wooden surface |
| `ref_beige_suede_street.png` | A style reference: warm outdoor street setting |
| `ref_blue_runner_onfoot.png` | A style reference: the product worn, in motion |

Any image format Pillow can open. Swapping in a different product means replacing the product
photo and rewriting the scene copy in `image_ad_scene_pool.json`, since the headlines are
written for this product.

## Choosing style references

The style reference supplies composition, lighting and mood. It is labelled "style only, never
its product" in the prompt, and that clause matters — without it the two products blend.

- **Use three or more that differ from each other.** Reusing one across a whole batch makes the
  set look like a single shoot, which defeats the point of a batch.
- **Look at each one before using it.** Skip any whose subject matter should not be reproduced
  into an ad.
- **Use images you have the right to use.** They are inputs to a commercial creative pipeline.

## Checking your setup

`--dry-run` does not open these files, so it passes whether or not they exist. To verify:

```bash
python execution/image_ad_creator/run_daily_image_ads.py --dry-run   # scene rotation only
python execution/image_ad_creator/generate_gemini_ad_batch.py \
    --spec execution/image_ad_creator/ai_runner_gemini_batch.json --out .tmp/check
```

The second names any missing file and exits before the spend gate, so it costs nothing.
