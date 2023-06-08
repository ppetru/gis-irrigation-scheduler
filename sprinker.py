#!/usr/bin/env python3

import argparse
import asyncio
import configparser
import pickle
import psycopg
import pyopensprinkler
import sys
from ortools.sat.python import cp_model
from collections import namedtuple
from math import lcm

Line = namedtuple('Line', ('name', 'interval', 'duration', 'splash'))


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

async def create_program(controller, name, stations, interval, remainder):
    debug(name + ': ')
    debug('create')
    await controller.create_program(name)
    program = get_program(controller, name)
    debugln('bail')
    return
    debug(', durations')
    durations = []
    for i in range(len(controller.stations)):
        durations.append(stations.get(i, 0))
    await program.set_station_durations(durations)
    debug(', enable')
    await program.set_enabled(True)
    debug(', weather')
    await program.set_use_weather_adjustments(1)
    debug(', schedule type')
    await program.set_program_schedule_type(3)  # interval-day
    debug(', schedule days')
    await program.set_schedule_interval_days(interval, remainder)
    debugln()


def stations_and_durations(station_map, lines):
    result = {}
    for line in lines:
        result[station_map[line.name]] = line.duration * 60
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
            await create_program(controller, name, stations, num_days, day_num - 1)
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
        cur = conn.execute("SELECT name, interval, duration, splash FROM " + config['database']['table'] + " WHERE interval IS NOT NULL")
        # convert the splash list into a tuple so we can use Line as dict keys
        lines = list(map(
            lambda x: Line._make(x[:3] + (tuple(x[3]),)),
            cur.fetchall()))
    return lines

def plan_schedule(config, lines):
    num_slots = int(config['irrigation']['daily_slots'])
    slot_minutes = int(config['irrigation']['slot_minutes'])

    line_dict = {}
    for index, l in enumerate(lines):
        line_dict[l.name] = index
    splash_lines = []
    for l in lines:
        new_splash = [line_dict[n] for n in l.splash]
        splash_lines.append(l._replace(splash=new_splash))
    num_lines = len(lines)

    intervals = []
    for l in lines:
        if l.interval not in intervals:
            intervals.append(l.interval)
    max_interval = lcm(*intervals)
    line_targets = []
    for l in lines:
        target = (max_interval // l.interval) * num_slots
        line_targets.append(target)

    num_days = max_interval * num_slots
    all_lines = range(num_lines)
    all_slots = range(num_slots)
    all_days = range(num_days)
    model = cp_model.CpModel()

    slots = {}
    for l in all_lines:
        for d in all_days:
            for s in all_slots:
                slots[(l, d, s)] = model.NewBoolVar('slot_l%id%is%i' % (l, d, s))

    ## hard constraints

    # limit slot duration
    for d in all_days:
        for s in all_slots:
            slot_tasks = []
            for l in all_lines:
                slot_tasks.append(lines[l].duration * slots[(l, d, s)])
            model.Add(sum(slot_tasks) <= slot_minutes)

    # meet line targets
    for l in all_lines:
        line_slots = []
        for d in all_days:
            for s in all_slots:
                line_slots.append(slots[(l, d, s)])
        model.Add(sum(line_slots) == line_targets[l])

    # line intervals and alternating slot
    def add_checked_implication(l, d, s, nd, ns, implied):
        if nd >= num_days or ns >= num_slots:
            return
        if implied:
            model.AddImplication(slots[(l, d, s)], slots[(l, nd, ns)])
        else:
            model.AddImplication(slots[(l, d, s)], slots[(l, nd, ns)].Not())

    for d in all_days:
        for s in all_slots:
            for l in all_lines:
                next_d = d + lines[l].interval
                next_s = (s + 1) % num_slots
                # a slot implies the correct next day and slot...
                add_checked_implication(l, d, s, next_d, next_s, True)
                # ...and none of the days and slots inbetween
                nd = d
                ns = s
                while True:
                    ns += 1
                    if ns == num_slots:
                        ns = 0
                        nd += 1
                    if nd == next_d and ns == next_s:
                        break
                    add_checked_implication(l, d, s, nd, ns, False)

    ## soft constraints

    # even out slot load
#    slotload = []
#    for d in all_days:
#        for s in all_slots:
#            tmp = model.NewIntVar(0, slot_minutes, "")
#            model.Add(tmp == sum([(slots[(l, d, s)]*lines[l].duration) for l in all_lines]))
#            slotload.append(tmp)
#    objective = model.NewIntVar(0, slot_minutes, "")
#    model.AddMinEquality(objective, slotload)
#    model.Maximize(objective)

    # minimize overlapping splash
    splashes = [0]
    for l in all_lines:
        for d in all_days:
            for s in all_slots:
                splash = [slots[(l, d, s)]]
                splash.extend(slots[(sp, d, s)] for sp in splash_lines[l].splash)
                tmp = model.NewIntVar(0, num_lines, "")
                model.Add(tmp == sum(splash))
                splashes.append(tmp)
    objective = model.NewIntVar(0, num_lines, "")
    model.AddMaxEquality(objective, splashes)
    model.Minimize(objective)

    solver = cp_model.CpSolver()
    solver.parameters.linearization_level = 0
    solver.parameters.enumerate_all_solutions = True

    status = solver.Solve(model)
    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        days_plan = []
        line_plan = {}
        for d in all_days:
            day_plan = []
            for s in all_slots:
                slot_plan = []
                for l in all_lines:
                    if solver.Value(slots[(l, d, s)]):
                       slot_plan.append(lines[l])
                       line_plan.setdefault(lines[l], []).append((d, s))
                day_plan.append(slot_plan)
            days_plan.append(day_plan)
        solution = [days_plan, line_plan]
    else:
        solution = None

    print('\nStatistics')
    print(f'  - status         : {solver.StatusName(status)}')
    print(f'  - objective      : {solver.ObjectiveValue()}')
    print(f'  - conflicts      : {solver.NumConflicts()}')
    print(f'  - branches       : {solver.NumBranches()}')
    print(f'  - wall time      : {solver.WallTime()} s')
    print()
    return solution

def print_schedule(day_plan, line_plan):
    print('*** Day plan:')
    for i, day in enumerate(day_plan):
        print(f'  Day {i}:')
        for j, slot in enumerate(day):
            slot_duration = 0
            for line in slot:
                slot_duration += line.duration
            print(f'    Slot {j} ({slot_duration : >3} minutes):')
            for line in slot:
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
