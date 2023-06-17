#!/usr/bin/env python3

import argparse
import asyncio
import configparser
import json
import pickle
import psycopg
import pyopensprinkler
import sys
from collections import namedtuple
from constraints import plan_schedule

Line = namedtuple('Line', ('name', 'interval', 'duration', 'group', 'splash'))


async def get_controller(config):
    controller = pyopensprinkler.Controller(
            config['opensprinkler']['controller'],
            config['opensprinkler']['password'])
    await controller.refresh()
    return controller


def get_program(controller, name):
    for program in controller.programs.values():
        if program.name == name:
            return program

def debug(s):
    print(s, end='')
    sys.stdout.flush()

def debugln(s):
    print(s)
    sys.stdout.flush()

async def create_program(controller, name, stations, interval, remainder, start_time):
    debug(name + ': ')
    # Manually build create program request, so that it can be done in one API call
    # with all the parameters set to the desired values.
    durations = []
    for i in range(len(controller.stations)):
        durations.append(stations.get(i, 0))
    # bit 0: program enable 'en' bit (1: enabled; 0: disabled)
    # bit 1: use weather adjustment 'uwt' bit (1: yes; 0: no)
    # bit 2-3: odd/even restriction (0: none; 1: odd-day restriction; 2: even-day restriction; 3: undefined)
    # bit 4-5: program schedule type (0: weekday; 1: undefined; 2: undefined; 3: interval day)
    # bit 6: start time type (0: repeating type; 1: fixed time type)
    # bit 7: enable date range (0: do not use date range; 1: use date range)
    flag = int('01110011', 2)
    # If (flag.bits[4..5]==3), this is an interval day schedule:
    #   days1 stores the interval day, days0 stores the remainder (i.e. starting in day).
    # For example, days1=3 and days0=0 means the program runs every 3 days, starting from today.
    assert interval > 0, "Interval must be nonzero"
    days0 = remainder
    days1 = interval
    start0 = start_time
    start1 = start2 = start3 = -1
    data = [
        flag,
        days0, days1,
        [start0, start1, start2, start3],
        durations
    ]
    params = {
        'pid': -1,
        'name': name,
        'v': json.dumps(data).replace(' ', ''),
    }
    await controller.request("/cp", params)
    debugln('done')


def stations_and_durations(station_map, lines):
    groups = {}
    scale_group = None
    scale_factor = 1
    # ensure that group durations match
    for line in lines:
        groups[line.group] = groups.get(line.group, 0) + line.duration
    # TODO: assuming two groups
    values = list(groups.values())
    keys = list(groups.keys())
    if values[0] != values[1]:
        if values[0] < values[1]:
            scale_group = keys[0]
            scale_factor = values[1] / values[0]
        else:
            scale_group = keys[1]
            scale_factor = values[0] / values[1]
    result = {}
    for line in lines:
        duration = line.duration * 60
        if line.group == scale_group:
            debug(f"Scaling {line.name}: {duration / 60 :g}m -> ")
            duration *= scale_factor
            debugln(f"{duration / 60 :g}m")
        result[station_map[line.name]] = duration
    return result


def get_name_prefix(config):
    name_prefix = config['irrigation'].get('program_name_prefix', '')
    if name_prefix:
        name_prefix += ' '
    return name_prefix


async def upload_schedule(config, day_plan):
    controller = await get_controller(config)
    station_map = {}
    for id, station in controller.stations.items():
        if station.enabled:
            station_map[station.name] = id
    name_prefix = get_name_prefix(config)
    num_days = len(day_plan)

    for day_num, slot_plan in enumerate(day_plan, start=1):
        for slot_num, line_plan in enumerate(slot_plan, start=1):
            stations = stations_and_durations(station_map, line_plan)
            slot_name = config['irrigation'].get(f'slot_{slot_num}_name', f'slot {slot_num}')
            name = f'{name_prefix}Day {day_num} {slot_name}'
            slot_time = int(config['irrigation'].get(f'slot_{slot_num}_time'))
            await create_program(controller, name, stations, num_days, day_num - 1, slot_time)
            await asyncio.sleep(1)

    await controller.session_close()


async def delete_program(controller, name):
    for idx, program in controller.programs.items():
        if program.name == name:
            print("Deleting", name)
            await controller.delete_program(idx)
            break


async def delete_autogen(config):
    controller = await get_controller(config)
    name_prefix = get_name_prefix(config)
    to_delete = []
    # We build the list of names first then iterate the programs again for each
    # deletion, because controller.delete_program takes an index that shifts
    # around when deleting
    for program in controller.programs.values():
        if program.name.startswith(name_prefix):
            to_delete.append(program.name)
    for name in to_delete:
        await delete_program(controller, name)
    await controller.session_close()


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
