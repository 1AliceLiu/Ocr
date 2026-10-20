import cv2
import easyocr

img = cv2.imread("/work/snapshots/1002754_018_main_native.jpg")
print("image:", img.shape)

crops = {
    "existing": img[971:1040, 43:219],
    "wide": img[930:1080, 0:420],
}
reader = easyocr.Reader(["en"], gpu=True, verbose=False)
print("device:", reader.device)
for name, roi in crops.items():
    path = f"/work/snapshots/_roi_{name}.png"
    cv2.imwrite(path, roi)
    det = reader.readtext(roi, allowlist="0123456789", detail=1, paragraph=False)
    print(name, roi.shape, [(d[1], round(float(d[2]), 3)) for d in det])
