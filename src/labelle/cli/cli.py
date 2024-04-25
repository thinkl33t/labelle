# === LICENSE STATEMENT ===
# Copyright (c) 2011 Sebastian J. Bronner <waschtl@sbronner.com>
#
# Copying and distribution of this file, with or without modification, are
# permitted in any medium without royalty provided the copyright notice and
# this notice are preserved.
# === END LICENSE STATEMENT ===
import argparse
import logging
import webbrowser
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps

from labelle import __version__
from labelle.lib.constants import (
    DEFAULT_MARGIN_PX,
    PIXELS_PER_MM,
    USE_QR,
    Align,
    BarcodeType,
    Justify,
    e_qrcode,
)
from labelle.lib.devices.device_manager import DeviceManager
from labelle.lib.devices.dymo_labeler import DymoLabeler
from labelle.lib.env_config import is_verbose_env_vars
from labelle.lib.font_config import NoFontFound, get_available_fonts, get_font_path
from labelle.lib.logger import configure_logging, set_not_verbose
from labelle.lib.render_engines import (
    BarcodeRenderEngine,
    BarcodeWithTextRenderEngine,
    HorizontallyCombinedRenderEngine,
    PictureRenderEngine,
    PrintPayloadRenderEngine,
    PrintPreviewRenderEngine,
    QrRenderEngine,
    RenderContext,
    RenderEngine,
    TestPatternRenderEngine,
    TextRenderEngine,
)
from labelle.lib.unicode_blocks import image_to_unicode
from labelle.lib.utils import system_run
from labelle.metadata import our_metadata

LOG = logging.getLogger(__name__)

FLAG_TO_STYLE = {
    "r": "regular",
    "b": "bold",
    "i": "italic",
    "n": "narrow",
}


class CommandLineUsageError(Exception):
    pass


def parse_args():
    # check for any text specified on the command line
    parser = argparse.ArgumentParser(description=our_metadata["Summary"])
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--text",
        action="append",
        help="Text Parameter, each parameter gives a new line",
    )
    parser.add_argument(
        "-f",
        "--frame-width-px",
        action="count",
        help="Draw frame around the text, more arguments for thicker frame",
    )
    parser.add_argument(
        "-s",
        "--style",
        choices=["r", "b", "i", "n"],
        default="r",
        help="Set fonts style (regular,bold,italic,narrow)",
    )
    parser.add_argument(
        "-a",
        "--align",
        choices=[align.value for align in Align],
        default=Align.LEFT,
        help="Align multiline text (left,center,right)",
    )
    parser.add_argument(
        "--test-pattern",
        type=int,
        default=0,
        help="Prints test pattern of a desired dot width",
    )

    length_options = parser.add_argument_group("Length options")

    length_options.add_argument(
        "-l",
        "--min-length",
        type=int,
        default=0,
        help="Specify minimum label length in mm",
    )
    length_options.add_argument(
        "--max-length",
        type=int,
        default=None,
        help="Specify maximum label length in mm, error if the label won't fit",
    )
    length_options.add_argument(
        "--fixed-length",
        type=int,
        default=None,
        help="Specify fixed label length in mm, error if the label won't fit",
    )

    length_options.add_argument(
        "-j",
        "--justify",
        choices=[justify.value for justify in Justify],
        default="center",
        help=(
            "Justify content of label if label content is less than the "
            "minimum or fixed length (left, center, right)"
        ),
    )
    parser.add_argument(
        "-u", "--font", nargs="?", help='Set user font, overrides "-s" parameter'
    )
    parser.add_argument(
        "-n",
        "--preview",
        action="store_true",
        help="Unicode preview of label, do not send to printer",
    )
    parser.add_argument(
        "--preview-inverted",
        action="store_true",
        help="Unicode preview of label, colors inverted, do not send to printer",
    )
    parser.add_argument(
        "--imagemagick",
        action="store_true",
        help="Preview label with Imagemagick, do not send to printer",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Preview label in the browser, do not send to printer",
    )
    parser.add_argument("--qr-content", type=str, help="Printing a QR-code")
    parser.add_argument(
        "--barcode-type",
        choices=[bt.value for bt in BarcodeType],
        type=str,
        help="The barcode type",
    )
    parser.add_argument(
        "-c",
        "--barcode",
        type=str,
        help="Printing a barcode",
    )
    parser.add_argument(
        "--barcode-with-text",
        action="store_true",
        help="The text under the barcode",
    )
    parser.add_argument("-p", "--picture", help="Print the specified picture")
    parser.add_argument(
        "-m",
        "--margin-px",
        type=int,
        default=DEFAULT_MARGIN_PX,
        help=f"Margin in px (default is {DEFAULT_MARGIN_PX})",
    )
    parser.add_argument(
        "--scale", type=int, default=90, help="Scaling font factor, [0,10] [%%]"
    )
    parser.add_argument(
        "-t",
        "--tape-size-mm",
        type=int,
        choices=[6, 9, 12, 19],
        default=12,
        help="Tape size: 6,9,12,19 mm, default=12mm",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Increase logging verbosity",
    )
    parser.add_argument(
        "--device",
        nargs="+",
        help=(
            "Select a particular device by filtering for a given substring "
            "in the device's manufacturer, product or serial number"
        ),
        type=str,
    )
    return parser.parse_args()


def mm_to_payload_px(mm, margin):
    """Convert a length in mm to a number of pixels of payload.

    The print resolution is 7 pixels/mm, and margin is subtracted from each side.
    """
    return max(0, (mm * PIXELS_PER_MM) - margin * 2)


def run():
    args = parse_args()

    if (not args.verbose) and (not is_verbose_env_vars()):
        # Neither --verbose flag nor the environment variable is set.
        set_not_verbose()

    # read config file
    style = FLAG_TO_STYLE.get(args.style)
    try:
        font_path = get_font_path(font=args.font, style=style)
    except NoFontFound as e:
        valid_font_names = [f.stem for f in get_available_fonts()]
        msg = f"{e}. Valid fonts are: {', '.join(valid_font_names)}"
        raise CommandLineUsageError(msg) from None

    # check if barcode, qrcode or text should be printed, use frames only on text
    if args.qr_content and not USE_QR:
        raise CommandLineUsageError(
            "QR code cannot be used without QR support " "installed"
        ) from e_qrcode

    if args.barcode_type and not args.barcode:
        raise CommandLineUsageError(
            "Cannot specify barcode type without a barcode value"
        )
    if args.barcode_with_text and not args.barcode:
        raise CommandLineUsageError(
            "Cannot specify barcode text without a barcode value"
        )

    if args.fixed_length is not None and (
        args.min_length != 0 or args.max_length is not None
    ):
        raise CommandLineUsageError(
            "Cannot't specify min/max and fixed length at the " "same time"
        )

    if args.max_length is not None and args.max_length < args.min_length:
        raise CommandLineUsageError("Maximum length is less than minimum length")

    render_engines: list[RenderEngine] = []

    if args.test_pattern:
        render_engines.append(TestPatternRenderEngine(args.test_pattern))

    if args.qr_content:
        render_engines.append(QrRenderEngine(args.qr_content))

    if args.barcode:
        barcode_render: RenderEngine
        if args.barcode_with_text:
            barcode_render = BarcodeWithTextRenderEngine(
                content=args.barcode,
                barcode_type=args.barcode_type,
                font_file_name=font_path,
                frame_width_px=args.frame_width_px,
            )
        else:
            barcode_render = BarcodeRenderEngine(
                content=args.barcode, barcode_type=args.barcode_type
            )
        render_engines.append(barcode_render)

    if args.text:
        render_engines.append(
            TextRenderEngine(
                text_lines=args.text,
                font_file_name=font_path,
                frame_width_px=args.frame_width_px,
                font_size_ratio=int(args.scale) / 100.0,
                align=args.align,
            )
        )

    if args.picture:
        render_engines.append(PictureRenderEngine(args.picture))

    if args.fixed_length is not None:
        min_label_mm_len = args.fixed_length
        max_label_mm_len = args.fixed_length
    else:
        min_label_mm_len = args.min_length
        max_label_mm_len = args.max_length

    margin_px = args.margin_px
    min_payload_len_px = mm_to_payload_px(min_label_mm_len, margin_px)
    max_payload_len_px = (
        mm_to_payload_px(max_label_mm_len, margin_px)
        if max_label_mm_len is not None
        else None
    )

    requires_device = not (
        args.preview or args.preview_inverted or args.imagemagick or args.browser
    )
    if not requires_device:
        device = None
    else:
        device_manager = DeviceManager()
        device_manager.scan()
        device = device_manager.find_and_select_device(patterns=args.device)
        device.setup()

    dymo_labeler = DymoLabeler(tape_size_mm=args.tape_size_mm, device=device)
    render_engine = HorizontallyCombinedRenderEngine(render_engines)
    render_context = RenderContext(
        background_color="white",
        foreground_color="black",
        height_px=dymo_labeler.height_px,
        preview_show_margins=False,
    )

    # print or show the label
    render: RenderEngine
    if requires_device:
        render = PrintPayloadRenderEngine(
            render_engine=render_engine,
            justify=args.justify,
            visible_horizontal_margin_px=margin_px,
            labeler_margin_px=dymo_labeler.labeler_margin_px,
            max_width_px=max_payload_len_px,
            min_width_px=min_payload_len_px,
        )
        bitmap, _ = render.render_with_meta(render_context)
        dymo_labeler.print(bitmap)
    else:
        render = PrintPreviewRenderEngine(
            render_engine=render_engine,
            justify=args.justify,
            visible_horizontal_margin_px=margin_px,
            labeler_margin_px=dymo_labeler.labeler_margin_px,
            max_width_px=max_payload_len_px,
            min_width_px=min_payload_len_px,
        )
        bitmap = render.render(render_context)
        LOG.debug("Demo mode: showing label...")
        if args.preview or args.preview_inverted:
            label_rotated = bitmap.transpose(Image.Transpose.ROTATE_270)
            print(image_to_unicode(label_rotated, invert=args.preview_inverted))
        if args.imagemagick:
            ImageOps.invert(bitmap).show()
        if args.browser:
            with NamedTemporaryFile(suffix=".png", delete=False) as fp:
                inverted = ImageOps.invert(bitmap.convert("RGB"))
                ImageOps.invert(inverted).save(fp)
                webbrowser.open(f"file://{fp.name}")


def main():
    configure_logging()
    with system_run():
        run()


if __name__ == "__main__":
    main()
