from ortools.sat.python import cp_model
from math import lcm


def plan_schedule(config, lines):
    num_slots = int(config["irrigation"]["daily_slots"])
    slot_minutes = int(config["irrigation"]["slot_minutes"])

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
                slots[(l, d, s)] = model.NewBoolVar("slot_l%id%is%i" % (l, d, s))

    ## hard constraints

    # limit slot duration
    for d in all_days:
        for s in all_slots:
            slot_tasks = []
            for l in all_lines:
                slot_tasks.append(lines[l].duration * slots[(l, d, s)])
            model.Add(sum(slot_tasks) <= slot_minutes)

    # equal time for each group within a slot
    # TODO: make the groups and group combos configurable
    group_mismatch = []
    for d in all_days:
        for s in all_slots:
            group_tasks = {
                "A": [],
                "B": [],
            }
            for l in all_lines:
                group_tasks[lines[l].group].append(lines[l].duration * slots[(l, d, s)])
            group_limit = slot_minutes // 2
            model.Add(sum(group_tasks["A"]) > 0)
            model.Add(sum(group_tasks["A"]) <= group_limit)
            model.Add(sum(group_tasks["B"]) > 0)
            model.Add(sum(group_tasks["B"]) <= group_limit)

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
    slotload = []
    for d in all_days:
        for s in all_slots:
            tmp = model.NewIntVar(0, slot_minutes, "")
            model.Add(
                tmp == sum([(slots[(l, d, s)] * lines[l].duration) for l in all_lines])
            )
            slotload.append(tmp)
    objective = model.NewIntVar(0, slot_minutes, "")
    model.AddMinEquality(objective, slotload)
    model.Maximize(objective)

    # minimize group mismatch
    groupdiff = []
    for d in all_days:
        for s in all_slots:
            group_tasks = {
                "A": [],
                "B": [],
            }
            for l in all_lines:
                group_tasks[lines[l].group].append(lines[l].duration * slots[(l, d, s)])
            group_limit = slot_minutes // 2
            tmp = model.NewIntVar(0, slot_minutes, "")
            model.AddAbsEquality(tmp, sum(group_tasks["A"]) - sum(group_tasks["B"]))
            groupdiff.append(tmp)
    objective = model.NewIntVar(0, slot_minutes, "")
    model.AddMaxEquality(objective, groupdiff)
    model.Minimize(objective)

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

    print("\nStatistics")
    print(f"  - status         : {solver.StatusName(status)}")
    print(f"  - objective      : {solver.ObjectiveValue()}")
    print(f"  - conflicts      : {solver.NumConflicts()}")
    print(f"  - branches       : {solver.NumBranches()}")
    print(f"  - wall time      : {solver.WallTime()} s")
    print()
    return solution
