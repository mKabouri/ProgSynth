from collections import defaultdict
import itertools
from typing import Dict, Generator, List, Set, Tuple, TypeVar
import copy
import argparse

import tqdm
import numpy as np

from dsl_loader import add_dsl_choice_arg, load_DSL

from synth import Dataset, PBE
from synth.generation.sampler import ListSampler, UnionSampler
from synth.pbe import reproduce_dataset
from synth.pruning import UseAllVariablesPruner
from synth.syntax import (
    CFG,
    ProbDetGrammar,
    enumerate_prob_grammar,
    DSL,
    Function,
    Primitive,
    Program,
    Variable,
    Arrow,
    Type,
)
from synth.pruning.type_constraints.utils import SYMBOL_ANYTHING, SYMBOL_FORBIDDEN
from synth.syntax.type_system import STRING
from synth.utils import chrono


parser = argparse.ArgumentParser(
    description="Generate a dataset copying the original distribution of another dataset"
)
add_dsl_choice_arg(parser)

parser.add_argument(
    "--dataset",
    type=str,
    default="{dsl_name}.pickle",
    help="dataset file (default: {dsl_name}.pickle)",
)
parser.add_argument("-s", "--seed", type=int, default=0, help="seed (default: 0)")
parser.add_argument(
    "--n", type=int, default=500, help="number of examples to be sampled (default: 500)"
)
parser.add_argument(
    "--max-depth",
    type=int,
    default=2,
    help="max depth of programs to check for (default: 2)",
)


parameters = parser.parse_args()
dsl_name: str = parameters.dsl
dataset_file: str = parameters.dataset.format(dsl_name=dsl_name)
input_checks: int = parameters.n
max_depth: int = parameters.max_depth
# ================================
# Constants
# ================================
DREAMCODER = "dreamcoder"
REGEXP = "regexp"
CALCULATOR = "calculator"
TRANSDUCTION = "transduction"
# ================================
# Initialisation
# ================================
dsl_module = load_DSL(dsl_name)
dsl, evaluator = dsl_module.dsl, dsl_module.evaluator

if dsl_name == REGEXP:
    from regexp.task_generator_regexp import reproduce_dataset
elif dsl_name == CALCULATOR:
    from calculator.calculator_task_generator import reproduce_dataset
elif dsl_name == TRANSDUCTION:
    from transduction.transduction_task_generator import reproduce_dataset
else:
    from synth.pbe import reproduce_dataset

# ================================
# Load dataset & Task Generator
# ================================
# Load dataset
print(f"Loading {dataset_file}...", end="")
with chrono.clock("dataset.load") as c:
    full_dataset: Dataset[PBE] = Dataset.load(dataset_file)
    print("done in", c.elapsed_time(), "s")
# Reproduce dataset distribution
print("Reproducing dataset...", end="", flush=True)
with chrono.clock("dataset.reproduce") as c:
    task_generator, _ = reproduce_dataset(
        full_dataset,
        dsl,
        evaluator,
        0,
        default_max_depth=max_depth,
        uniform_pgrammar=True,
    )
    print("done in", c.elapsed_time(), "s")
# We only get a task generator for the input generator
input_sampler = task_generator.input_generator
if dsl_name == TRANSDUCTION:
    l_s: ListSampler = input_sampler
    un_s: UnionSampler = l_s.element_sampler
    un_s.fallback = un_s.samplers[STRING]

# ================================
# Load dataset & Task Generator
# ================================
syntaxic_restrictions: Dict[str, Set[str]] = defaultdict(set)
specific_restrictions = set()
pattern_constraints = []

symmetrics: List[Tuple[str, Set[int]]] = []


stats = {s: {"total": 0, "syntaxic": 0} for s in ["identity", "constant", "equivalent"]}


# =========================================================================================
# Equivalence classes
# =========================================================================================

equivalence_classes = defaultdict(dict)
n_equiv_classes = 0


def new_equivalence_class(program: Program) -> None:
    global n_equiv_classes
    equivalence_classes[program] = n_equiv_classes
    n_equiv_classes += 1


def merge_equivalence_classes(program: Program, representative: Program) -> None:
    equivalence_classes[program] = equivalence_classes[representative]


def get_equivalence_class(num: int) -> Set[Program]:
    return {p for p, v in equivalence_classes.items() if v == num}


# =========================================================================================
# UTILS
# =========================================================================================

T = TypeVar("T")


def produce_all_variants(possibles: List[List[T]]) -> Generator[List[T], None, None]:
    # Enumerate all combinations
    n = len(possibles)
    maxi = [len(possibles[i]) for i in range(n)]
    current = [0 for _ in range(n)]
    while current[0] < maxi[0]:
        yield [possibles[i][j] for i, j in enumerate(current)]
        # Next combination
        i = n - 1
        current[i] += 1
        while i > 0 and current[i] >= maxi[i]:
            current[i] = 0
            i -= 1
            current[i] += 1


def vars(program: Program) -> List[Variable]:
    variables = []
    for p in program.depth_first_iter():
        if isinstance(p, Variable):
            variables.append(p)
    return variables


def constants(program: Program) -> List[Variable]:
    consts = []
    for p in program.depth_first_iter():
        if isinstance(p, Primitive) and not isinstance(p.type, Arrow):
            consts.append(p)
    return consts


def dump_primitives(program: Program) -> List[str]:
    pattern = []
    # Find all variables
    for p in program.depth_first_iter():
        if isinstance(p, Primitive):
            pattern.append(p.primitive)
    return pattern


# =========================================================================================
# =========================================================================================


def forbid_first_derivation(program: Program):
    pattern = dump_primitives(program)
    syntaxic_restrictions[pattern[0]].add(pattern[1])


def add_symmetric(program: Program) -> None:
    # global pattern_constraints
    variables = vars(program)
    varnos = [v.variable for v in variables]
    arguments = [v.type for v in sorted(variables, key=lambda k: k.variable)]
    rtype = program.type
    swap_indices = {
        varnos[i] != i and arg_type == rtype for i, arg_type in enumerate(arguments)
    }
    if len(swap_indices) == len(varnos):
        return

    primitive = dump_primitives(program)[0]
    symmetrics.append((primitive, swap_indices))


def add_constraint_for(program: Program, category: str):
    prog_vars = vars(program)
    max_vars = len(prog_vars)
    stats[category]["total"] += 1
    # If only one variable used, this is easy
    if max_vars == 1:
        forbid_first_derivation(program)
        stats[category]["syntaxic"] += 1
        return
    global specific_restrictions
    specific_restrictions.add(program)


def constant_program_analysis(program: Program):
    category = "constant"
    prog_consts = constants(program)
    max_consts = len(prog_consts)
    stats[category]["total"] += 1

    # If only one constant used, this is easy
    if max_consts == 1:
        forbid_first_derivation(program)
        stats[category]["syntaxic"] += 1
        return
    else:
        # TODO: this may be done better
        global specific_restrictions
        specific_restrictions.add(program)


# =========================================================================================
# =========================================================================================

sampled_inputs = {}
all_solutions = defaultdict(dict)
programs_done = set()


def init_base_primitives() -> None:
    # Pre Sample Inputs + Pre Execute base primitives
    for primitive in dsl.list_primitives:
        arguments = primitive.type.arguments()
        if len(arguments) == 0:
            continue
        if primitive.type not in sampled_inputs:
            sampled_inputs[primitive.type] = [
                [input_sampler.sample(type=arg) for arg in arguments]
                for _ in range(input_checks)
            ]
        inputs = sampled_inputs[primitive.type]
        base_program = Function(
            primitive,
            [Variable(i, arg_type) for i, arg_type in enumerate(arguments)],
        )
        solutions = [evaluator.eval(base_program, inp) for inp in inputs]
        all_solutions[base_program.type.returns()][base_program] = solutions
        new_equivalence_class(base_program)


def check_symmetries() -> None:
    iterable = tqdm.tqdm(dsl.list_primitives)
    for primitive in iterable:
        arguments = primitive.type.arguments()
        if len(arguments) == 0:
            continue
        iterable.set_postfix_str(primitive.primitive)

        base_program = Function(
            primitive,
            [Variable(i, arg_type) for i, arg_type in enumerate(arguments)],
        )
        inputs = sampled_inputs[primitive.type]
        all_sol = all_solutions[base_program.type.returns()]
        solutions = all_sol[base_program]

        # ========================
        # Symmetry+Identity part
        # ========================
        # Fill arguments per type
        arguments_per_type: Dict[Type, List[Variable]] = {}
        for i, arg_type in enumerate(arguments):
            if arg_type not in arguments_per_type:
                arguments_per_type[arg_type] = []
            arguments_per_type[arg_type].append(Variable(i, arg_type))
        # Enumerate all combinations
        for args in produce_all_variants([arguments_per_type[t] for t in arguments]):
            current_prog = Function(
                primitive,
                args,
            )
            programs_done.add(current_prog)
            is_symmetric = current_prog != base_program
            outputs = []
            for inp, sol in zip(inputs, solutions):
                out = evaluator.eval(current_prog, inp)
                outputs.append(out)
                if is_symmetric and out != sol:
                    is_symmetric = False
            if is_symmetric:
                add_symmetric(current_prog)
                merge_equivalence_classes(current_prog, base_program)
            else:
                new_equivalence_class(base_program)
                all_sol[current_prog] = outputs


def check_equivalent() -> None:
    simpler_pruner = UseAllVariablesPruner()
    ftypes = tqdm.tqdm(sampled_inputs.keys())
    for ftype in ftypes:
        # Add forbidden patterns to speed up search
        dsl.forbidden_patterns = copy.deepcopy(syntaxic_restrictions)
        cfg = CFG.depth_constraint(dsl, ftype, max_depth + 1)
        pcfg = ProbDetGrammar.uniform(cfg)

        inputs = sampled_inputs[ftype]
        all_sol = all_solutions[ftype.returns()]

        cfg_size = cfg.size()
        ftypes.set_postfix_str(f"{0 / cfg_size:.0%}")

        # ========================
        # Check all programs starting with max depth
        # ========================
        for done, program in enumerate(enumerate_prob_grammar(pcfg)):
            if program in programs_done or not simpler_pruner.accept((ftype, program)):
                continue
            is_constant = True
            my_outputs = []
            candidates = set(all_sol.keys())
            is_identity = len(program.used_variables()) == 1
            for i, inp in enumerate(inputs):
                out = evaluator.eval(program, inp)
                # Update candidates
                candidates = {c for c in candidates if all_sol[c][i] == out}
                if is_identity and out != inp[0]:
                    is_identity = False
                if is_constant and len(my_outputs) > 0 and my_outputs[-1] != out:
                    is_constant = False
                my_outputs.append(out)
            if is_identity:
                add_constraint_for(program, "identity")
            elif is_constant:
                add_constraint_for(program, "constant")
            elif len(candidates) > 0:
                merge_equivalence_classes(program, list(candidates)[0])
                add_constraint_for(program, "equivalent")
            else:
                new_equivalence_class(program)
                all_sol[program] = my_outputs
            if done & 256 == 0:
                ftypes.set_postfix_str(f"{done / cfg_size:.0%}")


def check_constants() -> None:
    types = set(
        primitive.type
        for primitive in dsl.list_primitives
        if not isinstance(primitive.type, Arrow)
    )
    for ty in types:
        all_evals = {evaluator.eval(p, []) for p in dsl.list_primitives if p.type == ty}
        # Add forbidden patterns to speed up search
        dsl.forbidden_patterns = copy.deepcopy(syntaxic_restrictions)
        cfg = CFG.depth_constraint(dsl, ty, max_depth + 1)
        pcfg = ProbDetGrammar.uniform(cfg)
        for program in enumerate_prob_grammar(pcfg):
            out = evaluator.eval(program, [])
            if out in all_evals:
                constant_program_analysis(program)


def exploit_symmetries() -> None:
    sym_types = defaultdict(set)
    name2P = {}
    for P in dsl.list_primitives:
        for name, _ in symmetrics:
            if P.primitive == name:
                sym_types[P.type.returns()].add(name)
                name2P[name] = P
                break
    for name, forbid_indices in symmetrics:
        elements = [name]
        P: Primitive = name2P[name]
        for i, arg in enumerate(P.type.arguments()):
            if i in forbid_indices:
                to_forbid = SYMBOL_FORBIDDEN + ",".join(sym_types[arg])
                elements.append(to_forbid)
            else:
                elements.append(SYMBOL_ANYTHING)
        pattern_constraints.append(" ".join(elements))


init_base_primitives()
check_symmetries()
check_equivalent()
check_constants()
exploit_symmetries()

print(f"Cache hit rate: {evaluator.cache_hit_rate:.1%}")
print()
print("[=== Report ===]")
for stat_name in stats:
    total = stats[stat_name]["total"]
    if total == 0:
        print(f"Found no {stat_name} program")
        continue
    ratio = stats[stat_name]["syntaxic"] / total
    print(
        "Found",
        total,
        stat_name,
        f"{ratio:.1%} of which were translated into constraints",
    )
if len(symmetrics) > 0:
    print(
        f"Found {len(symmetrics)} symmetries of which 100% were translated into constraints."
    )
else:
    print("Found no symmetries.")
print("[=== Results ===]")
print(
    f"Produced {sum(len(x) for x in syntaxic_restrictions.values())} forbidden patterns."
)
print(f"Produced {len(pattern_constraints)} type constraints.")

# SAving
with open(f"constraints_{dsl_name}.py", "w") as fd:
    fd.write("forbidden_patterns = {")
    for k, v in sorted(syntaxic_restrictions.items()):
        fd.write(f'"{k}":  {{ "' + '", "'.join(sorted(v)) + '"}, ')
    fd.write("}\n")
    fd.write("\n")
    fd.write("pattern_constraints = ")
    fd.write(str(pattern_constraints))
print(
    f"Found {n_equiv_classes} equivalence classes of which 0% were translated into constraints."
)
