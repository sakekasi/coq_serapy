#!/usr/bin/env python

import threading
import subprocess
import re
import os
import queue
import functools

from typing import Any, Dict, List, cast, Callable, Optional, Tuple

import pylspclient

from .contexts import ProofContext, Obligation
from .coq_backend import CoqBackend, UnrecognizedError, CoqException, CoqExn
from .coq_util import setup_opam_env
from .util import eprint

class QueuePipe(threading.Thread):

    def __init__(self, pipe):
        threading.Thread.__init__(self)
        self.pipe = pipe
        self.queue = queue.Queue()

    def run(self):
        line = self.pipe.readline().decode('utf-8')
        while line:
            self.queue.put(line)
            line = self.pipe.readline().decode('utf-8')
    def get(self) -> str:
        return self.queue.get()

def verbosePut(queue: queue.Queue, queue_name: str, msg: str) -> None:
    print(queue_name, ":", msg)
    queue.put(msg)

class CoqLSPyInstance(CoqBackend):
    proc: Any
    stderr_queue: QueuePipe
    messageQueues: Dict[str, queue.Queue]
    endpoint: pylspclient.LspEndpoint
    lsp_client: pylspclient.LspClient

    open_doc: str
    doc_version: int
    doc_sentences: List[str]
    state_dirty: bool
    cached_context: Optional[ProofContext]
    verbosity: int


    def __init__(self, lsp_command: str,
                 root_dir: Optional[str] = None,
                 timeout: int = 30, set_env: bool = True, verbosity: int = 0) -> None:
        if set_env:
            setup_opam_env()
        self.verbosity = verbosity
        self.proc = subprocess.Popen(lsp_command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     shell=True)
        self.stderr_queue = QueuePipe(self.proc.stderr)
        self.stderr_queue.start()

        queuedMessages = ['window/logMessage', '$/logTrace',
                          'textDocument/publishDiagnostics']
        printedMessages: List[str] = []
        ignoredMessages = ['$/coq/fileProgress']
        self.messageQueues = {msg_type: queue.Queue() for
                              msg_type in queuedMessages}

        self.endpoint  = pylspclient.LspEndpoint(
            pylspclient.JsonRpcEndpoint(self.proc.stdin, self.proc.stdout),
            notify_callbacks={**{msg_type: cast(Callable[[Any], None],
                                                functools.partial(queue.Queue.put,
                                                                  msgqueue))
                                                # functools.partial(verbosePut,
                                                #                   msgqueue, msg_type))
                                 for msg_type, msgqueue in self.messageQueues.items()},
                              **{msg_type: functools.partial(print, msg_type) for msg_type in printedMessages},
                              **{msg_type: lambda x: None for msg_type in ignoredMessages}},
            timeout=timeout)
        self.lsp_client = pylspclient.LspClient(self.endpoint)
        root_uri = root_dir or '.'
        workspace_folders = [{'name': 'coq-lsp', 'uri': root_uri}]
        capabilities: Dict[str, Any] = {}
        self.lsp_client.initialize(self.proc.pid, root_dir or '.', root_uri, None,
                                   capabilities,
                                   "off", workspace_folders)
        self.verify_init_messages()
        self.lsp_client.initialized()
        self.checkMessage("$/logTrace", '[process_queue]: Serving Request: initialized')

        self.state_dirty = True
        self.doc_sentences = []
        self._openEmptyDoc()

    def openDoc(self, filename: str) -> None:
        self.open_doc = filename
        docContents = ""

        self.doc_version = 1
        self.lsp_client.didOpen({"uri": self.open_doc,
                                 "languageId": "Coq",
                                 "version": self.doc_version,
                                 "text": docContents})
        msgs = [
            r'\[process_queue\]: Serving Request: textDocument/didOpen',
            r'\[process_queue\]: resuming document checking',
            r'\[check\]: resuming(?: \[v: \d+\])?, from: 0 l: \d+',
            r'\[check\]: done \[\d+\.\d+\]: document fully checked .*',
            r'\[cache\]: hashing: \d+.\d+ | parsing: \d+.\d+ | exec: \d+.\d+',
            r'\[cache\]: .*']
        for msg in msgs:
            self.checkMessagePattern('$/logTrace', msg)
        self._checkError()

    def _checkError(self) -> None:
        severe_errors = []
        try:
            while True: # Keep getting messages until the queue is empty
                error = self.messageQueues['textDocument/publishDiagnostics'].get_nowait()
                if error['version'] < self.doc_version:
                    eprint("Skipping error from an old doc version", guard=self.verbosity >= 2)
                    continue
                for message in error['diagnostics']:
                    if message['severity'] < 2 and message not in severe_errors:
                        assert error["version"] == self.doc_version,\
                            (error["version"], self.doc_version)
                        severe_errors.append(message)
        except queue.Empty as e:
            if len(severe_errors) > 0:
                exceptions = [self._handleError(message)
                              for message in severe_errors]
                raise exceptions[0] from e
            return
    def _handleError(self, message_json: Dict[str, Any]) -> CoqException:
        sentence_num, sentence = self._sentence_at_line(
            message_json['range']['start']['line'])
        eprint("Problem running statement: ", sentence, guard=self.verbosity >= 2)
        if sentence_num < len(self.doc_sentences):
            eprint(f"Rolling back {len(self.doc_sentences) - sentence_num} sentence(s)",
                   guard=self.verbosity >= 2)
            self.doc_sentences = self.doc_sentences[:sentence_num]
        msg_text = message_json['message']
        eprint(msg_text, guard=self.verbosity >= 2)
        if ("Cannot find a physical path bound to logical path"
             in msg_text):
            return CoqExn(msg_text)
        if re.match(r"The reference \S* was not found in the current environment\.",
                    msg_text):
            return CoqExn(msg_text)
        return UnrecognizedError(msg_text)

    # Uses 0-based line numbering, so the first line is line 0, the second is
    # line 1, etc.
    def _sentence_at_line(self, line: int) -> Tuple[int, str]:
        cur_line = 0
        for idx, sentence in enumerate(self.doc_sentences):
            sentence_lines = len(sentence.split("\n"))
            cur_line += sentence_lines
            if line < cur_line:
                return (idx, sentence)
        assert False, "Line number is after all the statements we have!"

    def _openEmptyDoc(self) -> None:
        self.openDoc("local1.v")

    def __enter__(self) -> 'CoqLSPyInstance':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        self.lsp_client.shutdown()
        self.lsp_client.exit()
        self.proc.terminate()

    def checkMessage(self, queue_name: str, message_text: str):
        message = self.messageQueues[queue_name].get()
        assert message['message'] == message_text, \
            f"Looking for message {message_text}, got message {message['message']}"

    def checkInMessage(self, queue_name: str, message_substring: str):
        message = self.messageQueues[queue_name].get()
        assert message_substring in message['message'],\
            f"Couldn't find substring {message_substring} in message {message['message']}"

    def checkMessagePattern(self, queue_name: str, message_pattern: str):
        message = self.messageQueues[queue_name].get()
        assert re.match(message_pattern, message['message']), \
            f"Message {message['message']} doesn't match pattern {message_pattern}"

    def verify_init_messages(self) -> None:
        self.checkMessage('window/logMessage', "Initializing server") # v0.1.4
        self.checkMessage('window/logMessage', "Server initialized") # v0.1.4

        self.checkInMessage('window/logMessage', "Configuration loaded") # v0.1.4
        expected_msgs = ['[init]: custom client options:',
                         '[init]: [init]: {}',
                         '[client_version]: any',
                         '[workspace]: initialized'] # v0.1.4

        for expected_msg in expected_msgs:
            self.checkMessage("$/logTrace", expected_msg)

    def addStmt(self, stmt: str, timeout:Optional[int] = None,
                force_update_nonfg_goals: bool = False) -> None:
        del force_update_nonfg_goals
        self.addStmt_noupdate(stmt, timeout)
        self.getProofContext()

    def addStmt_noupdate(self, stmt: str, timeout:Optional[int] = None) -> None:
        self.doc_sentences.append(stmt.strip("\n"))
        self.state_dirty = True

    def cancelLastStmt(self, cancelled: str, force_update_nonfg_goals: bool = False) -> None:
        del force_update_nonfg_goals
        self.doc_sentences.pop()
        self.state_dirty = True
    def cancelLastStmt_noupdate(self, cancelled: str) -> None:
        self.cancelLastStmt(cancelled)

    def updateState(self) -> None:
        pass

    def getProofContext(self) -> Optional[ProofContext]:
        if not self.state_dirty:
            return self.cached_context

        doc = "\n".join(self.doc_sentences)
        self.doc_version += 1
        self.lsp_client.didChange(
            {"uri": self.open_doc,
             "version": self.doc_version},
            [{"text": doc}])
        line = len(doc.split("\n")) - 1
        character = len(doc.split("\n")[-1]) if len(doc) > 0 else 0
        response = self.endpoint.call_method(
            "proof/goals", textDocument={"uri": self.open_doc},
            position={"line": line,
                      "character": character})
        parsed_response = parseGoalResponse(response)
        self._checkError()
        self.cached_context = parsed_response
        self.state_dirty = False
        return self.cached_context

    def isInProof(self) -> bool:
        return self.getProofContext() is not None

    def queryVernac(self, vernac: str) -> List[str]:
        raise NotImplementedError()
    def interrupt(self) -> None:
        raise NotImplementedError()
    def resetCommandState(self) -> None:
        self.doc_version += 1
        self.doc_sentences = []
        self.state_dirty = True


def parseObligation(obl_obj: Dict[str, Any]) -> Obligation:
    return Obligation([
        ", ".join(hyp_obj["names"]) + " : " + hyp_obj["ty"]
        for hyp_obj in obl_obj["hyps"]],
                      obl_obj["ty"])

def parseGoalResponse(response: Dict[str, Any]) -> Optional[ProofContext]:
    goals = response["goals"]
    if goals is None:
        return None
    return ProofContext([parseObligation(obl_obj)
                         for obl_obj in goals["goals"]],
                        [parseObligation(obl_obj)
                         for stack in goals["stack"]
                         for substack in stack
                         for obl_obj in substack],
                        [parseObligation(obl_obj)
                         for obl_obj in goals["shelf"]],
                        [parseObligation(obl_obj)
                         for obl_obj in goals["given_up"]])

def main():

    with CoqLSPyInstance("cd $HOME/research/coq-lsp && dune exec -- coq-lsp") as coq:
        print(coq.getProofContext())
        coq.addStmt("Theorem nat_refl : forall n : nat, n= n.")
        coq.addStmt("Proof.")
        coq.addStmt("intro.")
        print(coq.getProofContext())
        coq.cancelLastStmt()
        print(coq.getProofContext())
        coq.addStmt("induction n.")
        coq.addStmt("{")
        print(coq.getProofContext())


# Run main if this module is being run standalone
if __name__ == "__main__":
    main()
