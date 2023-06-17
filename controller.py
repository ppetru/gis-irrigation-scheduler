import asyncio
import pyopensprinkler
import json
import sys


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