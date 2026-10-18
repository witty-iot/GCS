# window_cleaning

Dataset + (eventually) training work for the `window_clean` action's dust
detection model: given a waypoint's camera feed of a window, decide whether
it needs cleaning and at what spray pressure.

## Dataset

Source: Roboflow Universe, "window check" by dirty-window —
https://universe.roboflow.com/dirty-window/window-check

- 865 images, labeled for `dirt`, `stain`, `glare`, `wet_glass`
- License: CC BY 4.0
- No pretrained model is published for this project — only the annotated
  dataset. A model needs to be trained on it (not just downloaded).

## Status

- [ ] Download dataset into `dataset/` (YOLOv8 format recommended — pick
      that format on the Roboflow download page, needs a free Roboflow
      account to export)
- [ ] Fine-tune a YOLOv8n detector on it
- [ ] Validate against real window photos (this dataset wasn't shot from a
      drone, so expect to need some of our own photos before this is
      reliable in the field)
- [ ] Wire the trained model into a `window_clean` action plugin

## Layout (once populated)

```
dataset/
  train/images/  train/labels/
  valid/images/  valid/labels/
  test/images/   test/labels/
  data.yaml
```
