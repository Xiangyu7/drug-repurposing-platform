from __future__ import annotations

import math
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
ASSETS_DIR = ROOT / "ability_deck_assets"
OUT_MD = ROOT / "个人能力介绍_视觉增强版.md"
OUT_PPTX = ROOT / "个人能力介绍_视觉增强版.pptx"

W = 1800
H = 820

BG = "#F6F1E8"
PAPER = "#FCFAF6"
NAVY = "#193549"
TEAL = "#2E6F6D"
RUST = "#C56A45"
GOLD = "#C9A66B"
SAGE = "#798D66"
MUTED = "#6C7A89"
LINE = "#D8CFC2"

FONT_REGULAR = "/System/Library/Fonts/Hiragino Sans GB.ttc"
FONT_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_LATIN = "/System/Library/Fonts/Avenir Next.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(path, size=size)


def latin_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_LATIN, size=size)


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> int:
    if not text:
        return 0
    left, _, right, _ = draw.textbbox((0, 0), text, font=fnt)
    return right - left


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        buf = ""
        for ch in paragraph:
            trial = buf + ch
            if not buf or text_width(draw, trial, fnt) <= max_width:
                buf = trial
            else:
                lines.append(buf)
                buf = ch
        if buf:
            lines.append(buf)
    return lines


def draw_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    fnt: ImageFont.FreeTypeFont,
    fill: str,
    max_width: int,
    line_gap: int = 12,
) -> int:
    x, y = xy
    lines = wrap_text(draw, text, fnt, max_width)
    bbox = draw.textbbox((0, 0), "测Ay", font=fnt)
    line_height = bbox[3] - bbox[1] + line_gap
    for idx, line in enumerate(lines):
        draw.text((x, y + idx * line_height), line, font=fnt, fill=fill)
    return y + max(len(lines), 1) * line_height


def background() -> Image.Image:
    image = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(image)
    draw.ellipse((W - 380, -120, W + 180, 440), fill="#EFE6DA")
    draw.ellipse((-180, H - 220, 380, H + 220), fill="#E9F0ED")
    draw.rectangle((0, 0, 32, H), fill=RUST)
    draw.line((80, 88, W - 90, 88), fill=LINE, width=2)
    draw.line((80, H - 72, W - 90, H - 72), fill=LINE, width=2)
    return image


def add_label(draw: ImageDraw.ImageDraw, text: str) -> None:
    draw.text((110, 34), text, font=latin_font(30), fill=RUST)


def rounded_card(
    image: Image.Image,
    box: tuple[int, int, int, int],
    fill: str = PAPER,
    outline: str | None = None,
    radius: int = 28,
    shadow: tuple[int, int, int, int] = (12, 12, 16, 50),
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    x1, y1, x2, y2 = box
    sx, sy, _, alpha = shadow
    od.rounded_rectangle((x1 + sx, y1 + sy, x2 + sx, y2 + sy), radius=radius, fill=(25, 53, 73, alpha))
    image.alpha_composite(overlay)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline or fill, width=2)


def draw_chip(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fill: str, text_fill: str) -> int:
    chip_font = font(24, bold=True)
    pad_x = 22
    pad_y = 14
    tw = text_width(draw, text, chip_font)
    h = 54
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + h), radius=20, fill=fill)
    draw.text((x + pad_x, y + pad_y - 2), text, font=chip_font, fill=text_fill)
    return x + tw + pad_x * 2 + 16


def slide_01() -> Path:
    image = background()
    draw = ImageDraw.Draw(image)
    add_label(draw, "ABILITY PROFILE")

    draw.text((110, 128), "把复杂的事", font=font(78, bold=True), fill=NAVY)
    draw.text((110, 222), "做成清晰结果", font=font(78, bold=True), fill=NAVY)
    draw.rectangle((110, 334, 260, 342), fill=RUST)
    draw_text_block(
        draw,
        "我最突出的能力，不是某一个单点技巧，而是从判断、推进到交付的完整闭环。",
        (110, 380),
        font(34),
        NAVY,
        720,
        line_gap=16,
    )

    card_w = 500
    card_h = 164
    cards = [
        ("看清问题", "先抓核心目标，再区分什么是真正重要，什么只是表面复杂。", TEAL),
        ("搭出路径", "把模糊任务拆成可执行的步骤、节点、标准和优先级。", RUST),
        ("推动落地", "持续推进直到形成可交付、可复盘、可继续使用的结果。", GOLD),
    ]
    start_x = 1120
    start_y = 118
    for idx, (title, body, color) in enumerate(cards):
        top = start_y + idx * (card_h + 28)
        rounded_card(image, (start_x, top, start_x + card_w, top + card_h))
        draw.rounded_rectangle((start_x + 24, top + 22, start_x + 146, top + 66), radius=18, fill=color)
        draw.text((start_x + 46, top + 30), title, font=font(26, bold=True), fill=PAPER)
        draw_text_block(draw, body, (start_x + 26, top + 86), font(27), NAVY, card_w - 56, line_gap=8)

    x = 110
    for label, fill, text_fill in [
        ("系统化思考", NAVY, PAPER),
        ("结果导向", TEAL, PAPER),
        ("风险意识", "#EADCCB", NAVY),
        ("协同推进", "#DCE8E3", NAVY),
        ("可复用性", "#EFE4D0", NAVY),
    ]:
        x = draw_chip(draw, x, 700, label, fill, text_fill)

    path = ASSETS_DIR / "slide_01.png"
    image.save(path)
    return path


def draw_radar(image: Image.Image, center: tuple[int, int], radius: int, labels: list[str], values: list[float]) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx, cy = center
    n = len(labels)
    levels = 5
    for level in range(1, levels + 1):
        pts = []
        r = radius * level / levels
        for i in range(n):
            angle = -math.pi / 2 + i * (2 * math.pi / n)
            pts.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
        od.polygon(pts, outline=LINE, fill=None)
    for i, label in enumerate(labels):
        angle = -math.pi / 2 + i * (2 * math.pi / n)
        x = cx + math.cos(angle) * (radius + 44)
        y = cy + math.sin(angle) * (radius + 44)
        od.line((cx, cy, cx + math.cos(angle) * radius, cy + math.sin(angle) * radius), fill=LINE, width=2)
        fnt = font(28, bold=True)
        tmp = ImageDraw.Draw(image)
        tw = text_width(tmp, label, fnt)
        tmp.text((x - tw / 2, y - 16), label, font=fnt, fill=NAVY)

    pts = []
    for i, val in enumerate(values):
        angle = -math.pi / 2 + i * (2 * math.pi / n)
        r = radius * val
        pts.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
    od.polygon(pts, fill=(46, 111, 109, 92), outline=TEAL)
    for x, y in pts:
        od.ellipse((x - 7, y - 7, x + 7, y + 7), fill=RUST)
    image.alpha_composite(overlay)


def slide_02() -> Path:
    image = background()
    draw = ImageDraw.Draw(image)
    add_label(draw, "CORE CAPABILITY")

    rounded_card(image, (90, 120, 1060, 730), fill="#FBF8F2")
    draw_radar(
        image,
        center=(560, 418),
        radius=238,
        labels=["结构化", "推进力", "风险感", "表达力", "标准感", "沉淀力"],
        values=[0.92, 0.90, 0.82, 0.84, 0.88, 0.86],
    )

    rounded_card(image, (1120, 130, 1710, 316))
    draw.text((1160, 164), "能力组合特征", font=font(38, bold=True), fill=NAVY)
    draw_text_block(
        draw,
        "不是平均分布，而是偏向于“看清问题 + 持续推进 + 高标准交付”的复合型优势。",
        (1160, 226),
        font(29),
        NAVY,
        500,
        line_gap=10,
    )

    strengths = [
        ("判断快", "能迅速抓到真正影响结果的核心变量。"),
        ("拆解清", "会把复杂目标拆成团队可执行的动作。"),
        ("推进稳", "面对变化时能调整路线，但不丢主线。"),
        ("表达准", "复杂内容能讲清楚，也能形成清晰输出。"),
    ]
    box_y = 352
    for idx, (title, body) in enumerate(strengths):
        top = box_y + idx * 92
        draw.rounded_rectangle((1138, top, 1686, top + 72), radius=22, fill="#EDE4D8" if idx % 2 == 0 else "#E5EFEC")
        draw.text((1160, top + 14), title, font=font(28, bold=True), fill=NAVY)
        draw.text((1290, top + 16), body, font=font(25), fill=NAVY)

    path = ASSETS_DIR / "slide_02.png"
    image.save(path)
    return path


def slide_03() -> Path:
    image = background()
    draw = ImageDraw.Draw(image)
    add_label(draw, "WORKFLOW")

    steps = [
        ("1", "厘清目标", "先把问题定义准确，确认最终要达成什么。"),
        ("2", "拆解问题", "把复杂任务拆成阶段、节点、交付物和依赖关系。"),
        ("3", "排序优先级", "先处理真正影响结果的部分，而不是平均用力。"),
        ("4", "设定节点", "为关键过程设置检查点，保证节奏和方向不偏。"),
        ("5", "推进协同", "把不同角色拉到同一判断上，减少沟通损耗。"),
        ("6", "复盘沉淀", "结果出来后形成方法和经验，而不是一次性完成。"),
    ]

    start_x = 150
    step_gap = 252
    y = 200
    for idx, (num, title, body) in enumerate(steps):
        x = start_x + idx * step_gap
        if idx < len(steps) - 1:
            draw.line((x + 90, y + 44, x + step_gap - 34, y + 44), fill=LINE, width=6)
        draw.ellipse((x, y, x + 88, y + 88), fill=TEAL if idx % 2 == 0 else RUST)
        draw.text((x + 31, y + 17), num, font=font(36, bold=True), fill=PAPER)
        draw.text((x - 6, y + 116), title, font=font(34, bold=True), fill=NAVY)
        rounded_card(image, (x - 34, y + 174, x + 170, y + 394), fill=PAPER)
        draw_text_block(draw, body, (x - 12, y + 204), font(26), NAVY, 154, line_gap=8)

    draw.text((116, 704), "我的工作方式更像经营一个闭环，而不是完成一个动作。", font=font(34, bold=True), fill=NAVY)

    path = ASSETS_DIR / "slide_03.png"
    image.save(path)
    return path


def slide_04() -> Path:
    image = background()
    draw = ImageDraw.Draw(image)
    add_label(draw, "TEAM VALUE")

    draw.text((110, 132), "我能给团队带来的，不只是执行力。", font=font(54, bold=True), fill=NAVY)
    draw.text((110, 198), "更重要的是把事情做得更清楚、更稳定、更可信。", font=font(36), fill=MUTED)

    cards = [
        ("让方向更清楚", "把模糊想法转成明确目标和判断依据。", TEAL, (110, 292, 840, 492)),
        ("让协作更顺畅", "在不同角色之间建立共同语言和推进节奏。", RUST, (900, 292, 1630, 492)),
        ("让执行更稳定", "为关键过程设标准、节点和边界，避免失控。", GOLD, (110, 530, 840, 730)),
        ("让结果更可信", "输出不仅能交付，还能回看、比较和复盘。", SAGE, (900, 530, 1630, 730)),
    ]
    for title, body, color, box in cards:
        rounded_card(image, box)
        x1, y1, x2, _ = box
        draw.rounded_rectangle((x1 + 28, y1 + 26, x1 + 182, y1 + 72), radius=18, fill=color)
        draw.text((x1 + 50, y1 + 34), "VALUE", font=latin_font(24), fill=PAPER)
        draw.text((x1 + 28, y1 + 102), title, font=font(40, bold=True), fill=NAVY)
        draw_text_block(draw, body, (x1 + 28, y1 + 154), font(29), NAVY, x2 - x1 - 56, line_gap=10)

    path = ASSETS_DIR / "slide_04.png"
    image.save(path)
    return path


def slide_05() -> Path:
    image = background()
    draw = ImageDraw.Draw(image)
    add_label(draw, "ROLE FIT")

    rounded_card(image, (86, 118, 1110, 734), fill="#FBF8F2")
    left = 196
    top = 208
    right = 998
    bottom = 640
    draw.line((left, top, left, bottom), fill=LINE, width=4)
    draw.line((left, bottom, right, bottom), fill=LINE, width=4)
    draw.text((left - 24, top - 54), "高", font=font(26, bold=True), fill=NAVY)
    draw.text((left - 24, bottom - 20), "低", font=font(26, bold=True), fill=NAVY)
    draw.text((right - 16, bottom + 16), "高", font=font(26, bold=True), fill=NAVY)
    draw.text((left - 62, top + 140), "协同复杂度", font=font(28, bold=True), fill=NAVY)
    draw.text((left + 260, bottom + 40), "任务不确定性", font=font(28, bold=True), fill=NAVY)

    roles = [
        ("复杂项目负责人", (702, 276), NAVY),
        ("关键推进者", (692, 508), RUST),
        ("桥梁型协同者", (424, 320), TEAL),
        ("质量把关者", (404, 522), SAGE),
    ]
    for label, (x, y), fill in roles:
        r = 88
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        lines = wrap_text(draw, label, font(24, bold=True), 118)
        yy = y - 18 * len(lines)
        for idx, line in enumerate(lines):
            tw = text_width(draw, line, font(24, bold=True))
            draw.text((x - tw / 2, yy + idx * 34), line, font=font(24, bold=True), fill=PAPER)

    rounded_card(image, (1170, 150, 1708, 702))
    draw.text((1208, 188), "最适合承担的场景", font=font(40, bold=True), fill=NAVY)
    scenes = [
        "从 0 到 1 的探索型任务",
        "跨团队、跨角色的复杂项目",
        "需要持续推进和反复校准的工作",
        "对结果质量和节奏要求都很高的事项",
    ]
    yy = 276
    for scene in scenes:
        draw.rounded_rectangle((1210, yy, 1668, yy + 82), radius=22, fill="#E6EFEC")
        draw.text((1240, yy + 20), "•", font=font(34, bold=True), fill=TEAL)
        draw.text((1272, yy + 22), scene, font=font(27), fill=NAVY)
        yy += 104

    path = ASSETS_DIR / "slide_05.png"
    image.save(path)
    return path


def slide_06() -> Path:
    image = background()
    draw = ImageDraw.Draw(image)
    add_label(draw, "DIFFERENTIATORS")

    draw.text((110, 128), "让我和别人拉开差距的地方", font=font(58, bold=True), fill=NAVY)

    pillars = [
        ("有判断", "先判断什么最重要，再决定怎么做。", NAVY),
        ("有标准", "关键环节会先定义边界、质量和完成标准。", TEAL),
        ("有节奏", "能在不确定和变化中保持推进，不轻易失速。", RUST),
        ("有沉淀", "倾向于把一次性经验变成可复用的方法。", GOLD),
    ]
    start_x = 104
    width = 388
    for idx, (title, body, fill) in enumerate(pillars):
        x1 = start_x + idx * 418
        rounded_card(image, (x1, 250, x1 + width, 626))
        draw.rounded_rectangle((x1 + 26, 274, x1 + 126, 356), radius=24, fill=fill)
        draw.text((x1 + 52, 296), f"{idx + 1}", font=font(38, bold=True), fill=PAPER)
        draw.text((x1 + 26, 394), title, font=font(42, bold=True), fill=NAVY)
        draw_text_block(draw, body, (x1 + 26, 466), font(29), NAVY, width - 52, line_gap=10)

    draw.rounded_rectangle((120, 692, 1680, 744), radius=26, fill="#1F4356")
    draw.text((318, 706), "能在不确定中保持推进，是我最稳定、也最稀缺的优势。", font=font(30, bold=True), fill=PAPER)

    path = ASSETS_DIR / "slide_06.png"
    image.save(path)
    return path


def slide_07() -> Path:
    image = Image.new("RGBA", (W, H), NAVY)
    draw = ImageDraw.Draw(image)
    draw.ellipse((W - 360, -120, W + 140, 360), fill="#244A5F")
    draw.ellipse((-240, H - 280, 320, H + 200), fill="#2B5F5D")
    draw.rectangle((0, 0, 40, H), fill=RUST)
    draw.text((110, 102), "SUMMARY", font=latin_font(34), fill="#E9D7C2")
    draw.text((110, 210), "我擅长的不是做一个点，", font=font(68, bold=True), fill=PAPER)
    draw.text((110, 308), "而是让一件复杂的事，", font=font(68, bold=True), fill=PAPER)
    draw.text((110, 406), "从想法走到结果。", font=font(68, bold=True), fill=PAPER)
    draw.rectangle((110, 548, 260, 556), fill=GOLD)
    draw_text_block(
        draw,
        "适合承担复杂任务、关键节点、跨角色协同，以及需要长期建设和持续推进的工作。",
        (110, 596),
        font(34),
        "#E6DDD0",
        1050,
        line_gap=16,
    )
    path = ASSETS_DIR / "slide_07.png"
    image.save(path)
    return path


def build_markdown(slides: list[tuple[str, Path]]) -> None:
    lines = [
        "# 个人能力介绍",
        "",
        f"![]({slides[0][1].relative_to(ROOT).as_posix()}){{width=96%}}",
    ]
    for title, path in slides[1:]:
        lines.extend(
            [
                "",
                f"# {title}",
                "",
                f"![]({path.relative_to(ROOT).as_posix()}){{width=96%}}",
            ]
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def post_process_pptx(path: Path) -> None:
    ns = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }

    replacements: dict[str, bytes] = {}
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        for name in names:
            if name == "ppt/presentation.xml":
                root = ET.fromstring(zf.read(name))
                sld_sz = root.find("p:sldSz", ns)
                if sld_sz is not None:
                    sld_sz.set("cx", "12192000")
                    sld_sz.set("cy", "6858000")
                replacements[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            elif name in {"ppt/theme/theme1.xml", "ppt/theme/theme2.xml"}:
                root = ET.fromstring(zf.read(name))
                for latin in root.findall(".//a:fontScheme//a:latin", ns):
                    latin.set("typeface", "Avenir Next")
                for ea in root.findall(".//a:fontScheme//a:ea", ns):
                    ea.set("typeface", "Hiragino Sans GB")
                for cs in root.findall(".//a:fontScheme//a:cs", ns):
                    cs.set("typeface", "Hiragino Sans GB")

                color_map = {
                    "accent1": "193549",
                    "accent2": "2E6F6D",
                    "accent3": "C56A45",
                    "accent4": "C9A66B",
                    "accent5": "798D66",
                    "accent6": "6C7A89",
                    "hlink": "2E6F6D",
                    "folHlink": "C56A45",
                }
                for key, value in color_map.items():
                    node = root.find(f".//a:clrScheme/a:{key}", ns)
                    if node is not None and list(node):
                        list(node)[0].set("val", value)
                replacements[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    temp_path = path.with_suffix(".tmp")
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = replacements.get(item.filename, src.read(item.filename))
            dst.writestr(item, data)
    temp_path.replace(path)


def generate() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    slides = [
        ("个人能力介绍", slide_01()),
        ("核心能力结构", slide_02()),
        ("我的工作方式", slide_03()),
        ("我能带来的团队价值", slide_04()),
        ("适配角色与场景", slide_05()),
        ("我的差异化优势", slide_06()),
        ("总结", slide_07()),
    ]
    build_markdown(slides)
    subprocess.run(["pandoc", str(OUT_MD.name), "-o", str(OUT_PPTX.name)], check=True, cwd=ROOT)
    post_process_pptx(OUT_PPTX)
    print(OUT_PPTX)


if __name__ == "__main__":
    generate()
