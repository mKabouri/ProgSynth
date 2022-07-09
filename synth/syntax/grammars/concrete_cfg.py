from collections import deque
from math import prod
from typing import Deque, Dict, Set, Tuple, List, Union
from dataclasses import dataclass, field

from synth.syntax.dsl import DSL
from synth.syntax.program import Constant, Primitive, Variable
from synth.syntax.type_system import Arrow, Type


@dataclass(frozen=True)
class NonTerminal:
    type: Type
    predecessors: List[Tuple[Union[Primitive, Variable], int]] = field(
        default_factory=lambda: []
    )
    depth: int = field(default=0)

    def __hash__(self) -> int:
        return hash((self.type, tuple(self.predecessors), self.depth))

    def __str__(self) -> str:
        return f"({self.type}, {self.depth}, {self.predecessors})"

    def __repr__(self) -> str:
        return self.__str__()


class ConcreteCFG:
    """
    Object that represents a context-free grammar with normalised probabilites

    start: a non-terminal

    rules: a dictionary of type {S: D}
    with S a non-terminal and D a dictionary {P : l} with P a program
    and l a list of non-terminals representing the derivation S -> P(S1,S2,..)
    with l = [S1,S2,...]

    hash_table_programs: a dictionary {hash: P}
    mapping hashes to programs
    for all programs appearing in rules

    """

    def __init__(
        self,
        start: NonTerminal,
        rules: Dict[
            NonTerminal, Dict[Union[Primitive, Variable, Constant], List[NonTerminal]]
        ],
        max_program_depth: int,
        clean: bool = True,
    ):
        self.start = start
        self.rules = rules
        self.max_program_depth = max_program_depth

        if clean:
            self.clean()

        # Find the type request
        type_req = self.start.type
        variables: List[Variable] = []
        for S in self.rules:
            for P in self.rules[S]:
                if isinstance(P, Variable):
                    if P not in variables:
                        variables.append(P)
        n = len(variables)
        for i in range(n):
            j = n - i - 1
            for v in variables:
                if v.variable == j:
                    type_req = Arrow(v.type, type_req)
        self.type_request = type_req

    def __hash__(self) -> int:
        return hash((self.start, str(self.rules), self.max_program_depth))

    def size(self) -> int:
        total_programs: Dict[NonTerminal, int] = {}
        for S in sorted(self.rules, key=lambda nt: nt.depth, reverse=True):
            total = 0
            for P in self.rules[S]:
                args_P = self.rules[S][P]
                if len(args_P) == 0:
                    total += 1
                else:
                    total += prod(total_programs[C] for C in args_P)
            total_programs[S] = total
        return total_programs[self.start]

    def clean(self) -> None:
        """
        remove non-terminals which do not produce programs.
        then remove non-terminals which are not reachable from the initial non-terminal.
        """
        self.__remove_non_productive__()
        self.__remove_non_reachable__()

    def __remove_non_productive__(self) -> None:
        """
        remove non-terminals which do not produce programs
        """
        new_rules: Dict[
            NonTerminal, Dict[Union[Primitive, Variable, Constant], List[NonTerminal]]
        ] = {}
        for S in reversed(self.rules):
            for P in self.rules[S]:
                args_P = self.rules[S][P]
                if all(arg in new_rules for arg in args_P):
                    if S not in new_rules:
                        new_rules[S] = {}
                    new_rules[S][P] = self.rules[S][P]

        for S in set(self.rules):
            if S in new_rules:
                self.rules[S] = new_rules[S]
            else:
                del self.rules[S]

    def __remove_non_reachable__(self) -> None:
        """
        remove non-terminals which are not reachable from the initial non-terminal
        """
        reachable: Set[NonTerminal] = set()
        reachable.add(self.start)

        reach: Set[NonTerminal] = set()
        new_reach: Set[NonTerminal] = set()
        reach.add(self.start)

        for _ in range(self.max_program_depth):
            new_reach.clear()
            for S in reach:
                for P in self.rules[S]:
                    args_P = self.rules[S][P]
                    for arg in args_P:
                        new_reach.add(arg)
                        reachable.add(arg)
            reach.clear()
            reach = new_reach.copy()

        for S in set(self.rules):
            if S not in reachable:
                del self.rules[S]

    def __str__(self) -> str:
        s = "Print a ConcreteCFG\n"
        s += "start: {}\n".format(self.start)
        for S in reversed(self.rules):
            s += "#\n {}\n".format(S)
            for P in self.rules[S]:
                s += "   {} - {}: {}\n".format(P, P.type, self.rules[S][P])
        return s

    def __eq__(self, o: object) -> bool:
        return (
            isinstance(o, ConcreteCFG)
            and o.type_request == self.type_request
            and o.rules == self.rules
        )

    @classmethod
    def from_dsl(
        cls,
        dsl: DSL,
        type_request: Type,
        max_depth: int,
        upper_bound_type_size: int = 10,
        min_variable_depth: int = 1,
        n_gram: int = 2,
        recursive: bool = False,
        constant_types: Set[Type] = set(),
    ) -> "ConcreteCFG":
        """
        Constructs a CFG from a DSL imposing bounds on size of the types
        and on the maximum program depth.

        max_depth: int - is the maxium depth of programs allowed
        uppder_bound_size_type: int - is the maximum size type allowed for polymorphic type instanciations
        min_variable_depth: int - min depth at which variables and constants are allowed
        n_gram: int - the context, a bigram depends only in the parent node
        recursvie: bool - allows the generated programs to call themselves
        constant_types: Set[Type] - the set of of types allowed for constant objects
        """
        dsl.instantiate_polymorphic_types(upper_bound_type_size)

        forbidden_sets: Dict[str, Set[str]] = {}
        for pattern in dsl.forbidden_patterns:
            if len(pattern) != 2:
                continue
            source, end = pattern[0], pattern[1]
            if source not in forbidden_sets:
                forbidden_sets[source] = set()
            forbidden_sets[source].add(end)

        if isinstance(type_request, Arrow):
            return_type = type_request.returns()
            args = type_request.arguments()
        else:
            return_type = type_request
            args = []

        rules: Dict[NonTerminal, Dict[Union[Variable, Primitive, Constant], List]] = {}

        list_to_be_treated: Deque[NonTerminal] = deque()
        list_to_be_treated.append(NonTerminal(return_type, [], 0))

        while len(list_to_be_treated) > 0:
            non_terminal = list_to_be_treated.pop()
            depth = non_terminal.depth
            current_type = non_terminal.type
            # a non-terminal is a triple (type, context, depth)
            # context is a list of (primitive, number_argument)

            # Create rule if non existent
            if non_terminal not in rules:
                rules[non_terminal] = {}

            if depth < max_depth:
                # Try to add variables rules
                if depth >= min_variable_depth:
                    for i in range(len(args)):
                        if current_type == args[i]:
                            var = Variable(i, current_type)
                            rules[non_terminal][var] = []
                    if current_type in constant_types:
                        cst = Constant(current_type)
                        rules[non_terminal][cst] = []
                # Try to add constants from the DSL
                for P in dsl.list_primitives:
                    type_P = P.type
                    if not isinstance(type_P, Arrow) and type_P == current_type:
                        rules[non_terminal][P] = []
                # Function call
                if depth < max_depth - 1:
                    forbidden = forbidden_sets.get(
                        non_terminal.predecessors[0][0].primitive
                        if len(non_terminal.predecessors) > 0
                        and isinstance(non_terminal.predecessors[0][0], Primitive)
                        else "",
                        set(),
                    )
                    # DSL Primitives
                    for P in dsl.list_primitives:
                        if P.primitive in forbidden:
                            continue
                        type_P = P.type
                        arguments_P = type_P.ends_with(current_type)
                        if arguments_P is not None:
                            decorated_arguments_P = []
                            for i, arg in enumerate(arguments_P):
                                addition: List[
                                    Tuple[Union[Primitive, Variable], int]
                                ] = [(P, i)]
                                new_predecessors = addition + non_terminal.predecessors
                                if len(new_predecessors) > n_gram - 1:
                                    new_predecessors.pop()
                                new_context = NonTerminal(
                                    arg, new_predecessors, depth + 1
                                )
                                decorated_arguments_P.append(new_context)
                                if new_context not in list_to_be_treated:
                                    list_to_be_treated.appendleft(new_context)

                            rules[non_terminal][P] = decorated_arguments_P

                    # Try to use variable as if there were functions
                    for vi, varg in enumerate(args):
                        arguments_V = varg.ends_with(current_type)
                        if arguments_V is not None:
                            V = Variable(vi, varg)
                            decorated_arguments_V = []
                            for i, arg in enumerate(arguments_V):
                                addition = [(V, i)]
                                new_predecessors = addition + non_terminal.predecessors
                                if len(new_predecessors) > n_gram - 1:
                                    new_predecessors.pop()
                                new_context = NonTerminal(
                                    arg, new_predecessors, depth + 1
                                )
                                decorated_arguments_V.append(new_context)
                                if new_context not in list_to_be_treated:
                                    list_to_be_treated.appendleft(new_context)

                            rules[non_terminal][V] = decorated_arguments_V
                    # Try to call self
                    if recursive:
                        arguments_self = type_request.ends_with(current_type)
                        if arguments_self is not None:
                            P = Primitive("@self", type_request)
                            decorated_arguments_self = []
                            for i, arg in enumerate(arguments_self):
                                addition = [(P, i)]
                                new_predecessors = addition + non_terminal.predecessors
                                if len(new_predecessors) > n_gram - 1:
                                    new_predecessors.pop()
                                new_context = NonTerminal(
                                    arg, new_predecessors, depth + 1
                                )
                                decorated_arguments_self.append(new_context)
                                if new_context not in list_to_be_treated:
                                    list_to_be_treated.appendleft(new_context)

                            rules[non_terminal][P] = decorated_arguments_self

        return ConcreteCFG(
            start=NonTerminal(return_type, [], 0),
            rules=rules,
            max_program_depth=max_depth,
            clean=True,
        )