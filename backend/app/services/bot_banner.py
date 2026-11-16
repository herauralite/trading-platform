from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

BLUE = "#42baff"


def banner(section, headline, detail):
    image = Image.new("RGB", (1080, 336))
    draw = ImageDraw.Draw(image)
    for x in range(1080):
        v = (x / 1080) ** 2
        draw.line((x, 0, x, 336), fill=(int(8+6*v), int(16+20*v), int(24+31*v)))
    for i in range(12):
        x, y = 770+i*17, 222-i*5
        draw.line([(x, 0), (x+110, 0), (x+110, y-65), (x+53, y-30), (x+53, 336)], fill=(21,54+i,72+i), width=2)
    for points, color in [
        ([(16,7),(33,7),(33,13),(28,13),(28,31),(22,31),(22,13),(16,13)], "#225571"),
        ([(3,7),(24,7),(24,13),(17,13),(17,31),(11,31),(11,13),(3,13)], BLUE),
    ]:
        draw.polygon([(40+x*1.8, 21+y*1.8) for x,y in points], fill=color)

    def put(x, y, value, size, color, bold=False):
        face = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        while True:
            try:
                font = ImageFont.truetype(face, size)
            except OSError:
                font = ImageFont.load_default(size=size)
            if draw.textlength(value, font=font) <= 1036-x or size <= 34:
                break
            size -= 2
        while value and draw.textlength(value, font=font) > 1036-x:
            value = value[:-2].rstrip() + "…"
        draw.text((x,y), value, fill=color, font=font, anchor="lt")
        return draw.textlength(value, font=font)

    width = put(113, 34, "Tali", 38, "#ebf4fa", True)
    put(113+width, 34, "Trade", 38, BLUE, True)
    draw.line((43,96,1037,96), fill="#263c4b", width=2)
    put(44,119,section.upper(),31,BLUE,True)
    put(40,169,headline,78,"#ebf4fa",True)
    put(44,273,detail,34,"#b0c7d6")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
