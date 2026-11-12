"""opex: invoice extraction harness. Commands: run, ui, learn."""
import argparse


def main() -> None:
    ap = argparse.ArgumentParser(prog="opex")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="extract every invoice under IN_DIR into OUT_DIR")
    p.add_argument("in_dir")
    p.add_argument("out_dir")
    p = sub.add_parser("ui", help="open the page for a batch, running or finished: steps, moving averages, review")
    p.add_argument("out_dir")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("learn", help="learn from saved reviews that were not learned yet")
    p.add_argument("out_dir")
    a = ap.parse_args()
    if a.cmd == "run":
        from . import harness

        harness.run(a.in_dir, a.out_dir)
    elif a.cmd == "ui":
        from . import ui

        ui.serve(a.out_dir, a.port)
    else:
        from . import learn

        learn.learn_batch(a.out_dir)
