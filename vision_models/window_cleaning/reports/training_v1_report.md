# Window Cleaning Vision Model — Training Report v1

**Date:** 2026-10-19
**Model:** YOLOv8n (nano), fine-tuned via transfer learning
**Classes:** `dirt`, `glare`, `stain`, `wet_glass`

## Dataset

- Source: Roboflow Universe, "window check" (dirty-window workspace), CC BY 4.0
- 798 training images, 67 validation images
- No `test` split provided — validation split used as proxy during training

## Training

- 50 epochs, batch size 16, image size 640
- Hardware: MacBook Pro M3 Pro, Apple MPS GPU backend
- Training time: 1.24 hours
- Weights: `runs/window_clean_v12/weights/best.pt` (5.6 MB)

## Results — Roboflow validation split (same source/style as training data)

| Class | Precision | Recall | mAP50 | mAP50-95 |
|---|---|---|---|---|
| **all** | 0.803 | 0.646 | **0.651** | 0.495 |
| dirt | 0.799 | 0.667 | 0.568 | 0.405 |
| glare | 0.691 | 0.486 | 0.504 | 0.219 |
| stain | 0.834 | 0.796 | 0.809 | 0.652 |
| wet_glass | 0.886 | 0.636 | 0.724 | 0.705 |

`stain` and `wet_glass` perform best. `glare` is weakest — inherently ambiguous
class (resembles lighting/reflection more than a fixed object), and both `dirt`
and `glare` suffer from having the fewest labeled instances in the validation set.

## Results — independent held-out test set (20 images, different source/style)

Images sourced from a separate Roboflow dataset (same label scheme), confirmed
zero filename overlap with training/validation data.

**Exact class-set match: 6/20 (30%)**

- Dominant failure pattern: model repeatedly predicts `stain` where ground truth
  is `glare` (6 of the 14 mismatches).
- 3 images with ground-truth `glare` produced zero detections.
- This test set skews toward wide building-facade/skyscraper photos with
  large-scale sun glare, a visually different style from the close-up window
  shots the model trained on — **this is a domain shift**, not purely a
  training failure. Performance on close-up window shots (matching the
  training distribution) is expected to track closer to the 65% validation
  result above.

## Conclusion

The model works and is directionally correct — `stain`/`wet_glass` detection
is solid, `glare` is the clear weak point both in-distribution and especially
out-of-distribution. Not yet production-ready; usable for a first demo/proof
of concept.

## Next steps to improve

1. Add more `glare` and `dirt` examples to training data, especially
   close-up window shots (the actual drone use case) rather than wide
   facade shots.
2. Train longer (100–150 epochs) and/or step up to `yolov8s` for more model
   capacity.
3. Re-check label quality on `glare`/`dirt` — weakest classes may have loose
   or inconsistent bounding boxes.
4. Build a held-out test set that matches the real drone camera distance/angle,
   not generic stock photos, before judging final accuracy.
