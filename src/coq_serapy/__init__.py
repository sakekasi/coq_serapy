#!/usr/bin/env python3
##########################################################################
#
#    This file is part of Proverbot9001.
#
#    Proverbot9001 is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Proverbot9001 is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Proverbot9001.  If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2019 Alex Sanchez-Stern and Yousef Alhessi
#
##########################################################################

import contextlib
import subprocess
import re

from typing import Iterator, List, Optional

from .util import eprint, parseSexpOneLevel
from .contexts import (ScrapedTactic, TacticContext, Obligation,
                       ProofContext, SexpObligation)
from .lsp_backend import main as lsp_main
from .lsp_backend import CoqLSPyInstance
from .serapi_backend import CoqSeraPyInstance
from .coq_util import (setup_opam_env, kill_comments,
                       preprocess_command, possibly_starting_proof,
                       summarizeContext, raise_,
                       normal_lemma_starting_patterns, parsePPSubgoal,
                       update_sm_stack, load_commands, get_stem,
                       load_commands_preserve)
from .coq_agent import TacticHistory, CoqAgent
from .coq_backend import (CoqBackend, CoqExn, BadResponse, AckError,
                          CompletedError, CoqTimeoutError,
                          UnrecognizedError, CoqAnomaly, CoqException,
                          ParseError, NoSuchGoalError, LexError)

def set_parseSexpOneLevel_fn(newfn) -> None:
    global parseSexpOneLevel
    parseSexpOneLevel = newfn

@contextlib.contextmanager
def CoqContext(prelude: str = ".", verbosity: int = 0) -> Iterator[CoqAgent]:
    setup_opam_env()
    version_string = subprocess.run(["coqc", "--version"], stdout=subprocess.PIPE,
                                    text=True, check=True).stdout
    version_match = re.fullmatch(r"\d+\.(\d+).*", version_string,
                                 flags=re.DOTALL)
    assert version_match
    minor_version = int(version_match.group(1))
    assert minor_version >= 10, \
            "Versions of Coq before 8.10 are not supported! "\
            f"Currently installed coq is {version_string}"

    backend: CoqBackend
    try:
        if minor_version < 16:
            backend = CoqSeraPyInstance(["sertop", "--implicit"], root_dir=prelude)
        else:
            backend = CoqLSPyInstance("coq-lsp", root_dir=prelude)
        agent = CoqAgent(backend, prelude, verbosity=verbosity)
    except CoqAnomaly:
        eprint("Anomaly during initialization! Something has gone horribly wrong.")
        raise

    try:
        yield agent
    finally:
        agent.backend.close()

# Backwards Compatibility (to some extent)
SerapiInstance = CoqAgent
@contextlib.contextmanager
def SerapiContext(coq_commands: List[str], module_name: Optional[str],
                  prelude: str, _use_hammer: bool = False,
                  _log_outgoing_messages: Optional[str] = None) \
                  -> Iterator[CoqAgent]:
    try:
        backend = CoqSeraPyInstance(coq_commands, root_dir=prelude)
        agent = CoqAgent(backend, prelude)
        if module_name and module_name not in ["Parameter", "Prop", "Type"]:
            agent.run_stmt(f"Module {module_name}.")
    except CoqAnomaly:
        eprint("Anomaly during initialization! Something has gone horribly wrong.")
        raise
    try:
        yield agent
    finally:
        agent.backend.close()
