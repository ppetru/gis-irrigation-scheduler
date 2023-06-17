#!/usr/bin/env python3

import argparse
import asyncio
import configparser
import pickle
import psycopg
from collections import namedtuple
from constraints import plan_schedule
from controller import upload_schedule, delete_autogen

Line = namedtuple('Line', ('name', 'interval', 'duration', 'group', 'splash'))


def get_lines(config):
    with psycopg.connect(config['database']['config']) as conn:
        conn.execute("SET client_encoding TO utf8")
        cur = conn.execute("SELECT name, interval, duration, \"group\", splash FROM " + config['database']['table'] + " WHERE interval IS NOT NULL")
        # convert the splash list into a tuple so we can use Line as dict keys
        lines = list(map(
            lambda x: Line._make(x[:4] + (tuple(x[4]),)),
            cur.fetchall()))
    return lines


def print_schedule(day_plan, line_plan):
    print('*** Day plan:')
    for i, day in enumerate(day_plan):
        print(f'  Day {i}:')
        for j, slot in enumerate(day):
            slot_duration = 0
            group_duration = {}
            for line in slot:
                slot_duration += line.duration
                group_duration[line.group] = group_duration.get(line.group, 0) + line.duration
            print(f'    Slot {j} ({slot_duration : >3} minutes):')
            for group in sorted(group_duration.keys()):
                print(f'      Group {group} ({group_duration[group]} minutes):')
                for line in filter(lambda l: l.group == group, slot):
                    print(f'      {line.name : >25} ({line.duration}m every {line.interval}d)')
    print()
    print('*** Line plan:')
    for line, plan in line_plan.items():
        print(f'  {line.name} (every {line.interval}d): ', end='')
        for day, slot in plan:
            print(f'day {day : >2} slot {slot}, ', end='')
        print()
        for splash in line.splash:
            overlaps = []
            for splash_line in line_plan.keys():
                if splash_line.name == splash:
                    for (pd, ps), (sd, ss) in zip(plan, line_plan[splash_line]):
                        if pd == sd and ps == ss:
                            overlaps.append((pd, ps))
            if overlaps:
                print(f'    overlapping {splash} on: ', end='')
                for d, s in overlaps:
                    print(f' day {d} slot {s}, ', end='')
                print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_file', '-c', default='config.ini', help='Where to read configuration from. Defaults to config.ini')
    parser.add_argument('--write_file', '-w', help='Generate schedule and write it to this file')
    parser.add_argument('--read_file', '-r', help='Read schedule from this file instead of generating it')
    parser.add_argument('--print', '-p', default=False, action='store_true', help='Print schedule')
    parser.add_argument('--upload', '-u', default=False, action='store_true', help='Upload schedule to controller')
    parser.add_argument('--delete', '-d', default=False, action='store_true', help='Delete autogenerated schedule from controller')
    args = parser.parse_args()
    config = configparser.ConfigParser()
    config.read(args.config_file)

    lines = get_lines(config)

    if args.read_file is not None:
        with open(args.read_file, 'rb') as f:
            schedule = pickle.load(f)
    else:
        schedule = plan_schedule(config, lines)
    if not schedule:
        print('No schedule found.')
        return

    if args.print:
        print_schedule(*schedule)

    if args.write_file is not None:
        with open(args.write_file, 'wb') as f:
            pickle.dump(schedule, f)

    if args.upload:
        asyncio.run(upload_schedule(config, schedule[0]))
    elif args.delete:
        asyncio.run(delete_autogen(config))


if __name__ == '__main__':
    main()
