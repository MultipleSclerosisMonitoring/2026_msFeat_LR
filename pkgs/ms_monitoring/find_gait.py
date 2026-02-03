import argparse
import json
from typing import Optional, List
from datetime import datetime, timedelta, timezone

from msTools import i18n
from msTools.data_manager import DataManager
from msGait.movement_detector import MovementDetector


class VAction(argparse.Action):
    """
    Custom argparse Action to handle cumulative verbosity (-v).
    """
    def __call__(self, parser, namespace, values, option_string=None):
        if values is None:
            setattr(namespace, self.dest, getattr(namespace, self.dest) + 1)
        else:
            setattr(namespace, self.dest, int(values))

def parse_range_list(rango_str):
    """Convert a string like '1-271' or '1,5,10-15' into a list of integers.

    Args:
    rango_str: Range/list specification.

    Returns:
    List[int]: Sorted list of IDs.
    """
    result = set()
    
    # Dividir por comas para manejar múltiples segmentos (ej. '1,10-15')
    for segment in rango_str.split(','):
        if not segment:
            continue
        
        # Si contiene un guion, es un rango (ej. '10-15')
        if '-' in segment:
            try:
                # Extraer inicio y fin del rango
                start, end = map(int, segment.split('-'))
                # Añadir el rango de enteros al resultado
                result.update(range(start, end + 1))
            except ValueError:
                raise ValueError(f"Formato de rango inválido: {segment}. Debe ser 'inicio-fin'.")
        
        # Si no hay guion, es un solo ID (ej. '5')
        else:
            try:
                result.add(int(segment))
            except ValueError:
                raise ValueError(f"Formato de ID inválido: {segment}. Debe ser un número.")
                
    return sorted(list(result)) # Devolver la lista ordenada


def _default_last_hours_window(hours_back: int) -> tuple[str, str]:
    """Return ISO timestamps (UTC) for the last ``hours_back`` hours window.

    Args:
    hours_back: Number of hours to look back from *now*.

    Returns:
    Tuple[str, str]: (fstart, fend) as ISO strings with "Z" suffix.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    # Use Zulu format acceptable by Influx/our pipeline downstream
    fstart = start.isoformat().replace("+00:00", "Z")
    fend = now.isoformat().replace("+00:00", "Z")
    return fstart, fend



def main():
    # 1) Pre‐parse only -l/--lang (to avoid showing help prematurely)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "-l", "--lang", dest="lng", type=str, default="es",
        help="Language: [en|es]"
    )
    pre_args, remaining = pre.parse_known_args()

    # 2) Initialize translation using selected language
    i18n.init_translation(pre_args.lng)
    _ = i18n._

    # 3) Build the real ArgumentParser, using _() for all user‐facing text
    parser = argparse.ArgumentParser(
        description=_("ARG_TIT_FIND_GAIT")
    )
    parser.add_argument(
        "-i", "--ids", dest="act_all_ids", metavar="IDS", type=parse_range_list, 
        required=False,
        help=_("ARG_LIST_ACT_ALL_IDS. Formato 1-271 o 1,5,10-15")
    )
    parser.add_argument(
        "-c", "--config", dest="config_file", type=str, required=True,
        help=_("ARG_STR_PATH_YAML")
    )
    parser.add_argument(
        "-l", "--lang", dest="lng", type=str, default=pre_args.lng,
        help=_("ARG_STR_LNG")
    )
    parser.add_argument(
        "-o", "--output", dest="fout", type=str, default=None,
        help=_("ARG_STR_FOUT")
    )
    parser.add_argument(
        "-v", "--verbose", action=VAction, nargs="?", default=0, const=1,
        help=_("ARG_VB_LEVEL")
    )
    parser.add_argument(
        "--head-rows", dest="head_rows", type=int, default=8,
        help=_("ARG_HEAD_ROWS")
    )
    parser.add_argument(
        "--save", dest="save", type=int, choices=[0,1], default=1,
        help=_("ARG_SAVE")
    )
    parser.add_argument(
        "--hours_back", dest="hours_back", type=int, default=25,
        help=_("ARG_HBACK")
    )
    # 4) Parse the remaining arguments
    args = parser.parse_args(remaining)

    # 5) If language was changed here, re‐initialize translation
    if args.lng != pre_args.lng:
        i18n.init_translation(args.lng)
        _ = i18n._

    # Determine retrieval mode: explicit IDs vs last-N-hours window
    use_ids = args.act_all_ids is not None and len(args.act_all_ids) > 0
    fstart, fend = (None, None)


    if not use_ids:
        # Fallback: last N hours window (defaults to 25h)
        fstart, fend = _default_last_hours_window(args.hours_back)
        if args.verbose >= 1:
            print(_("FGAIT_USING_LAST_HOURS").format(n=args.hours_back))

    # Initialize the MovementDetector (internally handles DataManager and segment retrieval)
    # Supports either supplying IDs or date ranges
    detector = MovementDetector(
        config_file   = args.config_file,
        sampling_rate = 50,
        fstart        = fstart,
        fend          = fend,
        ids           = (args.act_all_ids if use_ids else None),
        verbose       = args.verbose
    )

    # If no leg data was retrieved, exit
    if detector.df_legs.empty:
        return

    if args.verbose >= 1:
        print(_("FGAIT_1ST"))

    # Detect effective movements per leg
    df_effective = detector.detect_effective_movement(
        detector.df_legs,
        args.fout,
        args.verbose
    )
    if df_effective.empty:
        print(_("FGAIT_NO_WALK"))
        return

    if args.verbose >= 2:
        print(_("FGAIT_WKLS_FND"))
        print(df_effective.head(args.head_rows))

    # Optionally save effective_movement to PostgreSQL
    if args.save == 1:
        detector.save_to_postgresql("effective_movement", df_effective, args.verbose)
        if args.verbose >= 1:
            print(_("FGAIT_NUM_WALKS").format(ns=len(df_effective)))

    # Detect simultaneous effective gait periods (both feet)
    df_gait = detector.detect_effective_gait(df_effective, args.verbose)
    if df_gait.empty:
        if args.verbose >= 1:
            print(_("NO_GAIT_PERIODS"))
    else:
        if args.verbose >= 1:
            print(_("GAIT_PERIODS_HEADER"))
            if args.verbose >= 2:
                # Indent the DataFrame for clearer display
                df_string = df_gait.to_string(index=False)
                indentation = "     "
                indented = "\n".join(indentation + line for line in df_string.splitlines())
                print(indented)

        if args.save == 1:
            detector.save_to_postgresql("effective_gait", df_gait, args.verbose)
            if args.verbose >= 1:
                print(_("GAIT_SAVED_COUNT").format(n=len(df_gait)))

    if args.verbose >= 1:
        print(_("FGAIT_END"))

    detector.close()


if __name__ == "__main__":
    main()
