# Copyright 2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""
This module contains a function to run aa custom PennyLane VQE problem on qiskit runtime.
"""

import os
import inspect
from collections import OrderedDict

import numpy as np
import pennylane as qml

from pennylane_qiskit.qiskit_device import QiskitDevice
import qiskit.circuit.library.n_local as lib_local
from qiskit.providers.ibmq.runtime import ResultDecoder
from qiskit.circuit import ParameterVector, QuantumCircuit, QuantumRegister
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit import IBMQ
from qiskit.providers.ibmq.exceptions import IBMQAccountError

from scipy.optimize import OptimizeResult


class VQEResultDecoder(ResultDecoder):
    """ """

    @classmethod
    def decode(cls, data):
        data = super().decode(data)
        return OptimizeResult(data)


class RuntimeJobWrapper:
    """A simple Job wrapper that attaches interm results directly to the job object itself
    in the `interm_results attribute` via the `_callback` function.
    """

    def __init__(self):
        self._job = None
        self._decoder = VQEResultDecoder
        self.interm_results = []

    def _callback(self, xk):
        """The callback function that attaches interm results:

        Parameters:
            xk (array_like): A list or NumPy array to attach.
        """
        self.interm_results.append(xk)

    def __getattr__(self, attr):
        if attr == "result":
            return self.result
        else:
            if attr in dir(self._job):
                return getattr(self._job, attr)
            raise AttributeError("Class does not have {}.".format(attr))

    def result(self):
        """Get the result of the job as a SciPy OptimizerResult object.

        This blocks until job is done, cancelled, or errors.

        Returns:
            OptimizerResult: A SciPy optimizer result object.
        """
        return self._job.result(decoder=self._decoder)


def vqe_runner(
    backend,
    hamiltonian,
    x0,
    program_id=None,
    ansatz="EfficientSU2",
    ansatz_config={},
    optimizer="SPSA",
    optimizer_config={"maxiter": 100},
    shots=8192,
    use_measurement_mitigation=False,
    **kwargs
):
    """Routine that executes a given VQE problem via the sample-vqe program on the target backend.

    Parameters:
        backend (ProgramBackend): Qiskit backend instance.
        hamiltonian (list): Hamiltonian whose ground state we want to find.
        program_id(str): Optional, if the program is already uploaded.
        ansatz (Quantum function or str): Optional, name of ansatz quantum circuit to use, default='EfficientSU2'
        ansatz_config (dict): Optional, configuration parameters for the ansatz circuit.
        x0 (array_like): Optional, initial vector of parameters.
        optimizer (str): Optional, string specifying classical optimizer, default='SPSA'.
        optimizer_config (dict): Optional, configuration parameters for the optimizer.
        shots (int): Optional, number of shots to take per circuit.
        use_measurement_mitigation (bool): Optional, use measurement mitigation, default=False.

    Returns:
        OptimizeResult: The result in SciPy optimization format.
    """

    token = kwargs.get("ibmqx_token", None) or os.getenv("IBMQX_TOKEN")
    url = kwargs.get("ibmqx_url", None) or os.getenv("IBMQX_URL")

    if token is not None:
        # token was provided by the user, so attempt to enable an
        # IBM Q account manually
        ibmq_kwargs = {"url": url} if url is not None else {}
        IBMQ.enable_account(token, **ibmq_kwargs)
    else:
        # check if an IBM Q account is already active.
        #
        # * IBMQ v2 credentials stored in active_account().
        #   If no accounts are active, it returns None.

        if IBMQ.active_account() is None:
            # no active account
            try:
                # attempt to load a v2 account stored on disk
                IBMQ.load_account()
            except IBMQAccountError:
                # attempt to enable an account manually using
                # a provided token
                raise IBMQAccountError(
                    "No active IBM Q account, and no IBM Q token provided."
                ) from None

    provider = IBMQ.get_provider(hub="ibm-q", group="open", project="main")

    if program_id is None:

        meta = {
            "name": "vqe-runtime",
            "description": "A sample VQE program.",
            "max_execution_time": 100000,
            "spec": {},
        }

        meta["spec"]["parameters"] = {
            "$schema": "https://json-schema.org/draft/2019-09/schema",
            "properties": {
                "hamiltonian": {
                    "description": "Hamiltonian whose ground state we want to find.",
                    "type": "array",
                },
                "ansatz": {
                    "description": "Name of ansatz quantum circuit to use, default='EfficientSU2'",
                    "type": "string",
                    "default": "EfficientSU2",
                },
                "ansatz_config": {
                    "description": "Configuration parameters for the ansatz circuit.",
                    "type": "object",
                },
                "optimizer": {
                    "description": "Classical optimizer to use, default='SPSA'.",
                    "type": "string",
                    "default": "SPSA",
                },
                "x0": {
                    "description": "Initial vector of parameters. This is a numpy array.",
                    "type": "array",
                },
                "optimizer_config": {
                    "description": "Configuration parameters for the optimizer.",
                    "type": "object",
                },
                "shots": {
                    "description": "The number of shots used for each circuit evaluation.",
                    "type": "integer",
                },
                "use_measurement_mitigation": {
                    "description": "Use measurement mitigation, default=False.",
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["hamiltonian"],
        }

        meta["spec"]["return_values"] = {
            "$schema": "https://json-schema.org/draft/2019-09/schema",
            "description": "Final result in SciPy optimizer format",
            "type": "object",
        }

        meta["spec"]["interim_results"] = {
            "$schema": "https://json-schema.org/draft/2019-09/schema",
            "description": "Parameter vector at current optimization step. This is a numpy array.",
            "type": "array",
        }

        program_id = provider.runtime.upload_program(
            data="pennylane_qiskit/runtime/vqe_runtime.py", metadata=meta
        )

    options = {"backend_name": backend}

    inputs = {}

    # Num qubits from hamiltonian

    _, observables = hamiltonian.terms

    qubit_set = set()
    for obs in observables:
        for qubit in obs.wires.tolist():
            qubit_set.add(qubit)

    num_qubits_h = list(qubit_set)[-1] + 1

    # Validate circuit ansatz and number of qubits
    if not isinstance(ansatz, str):

        if isinstance(ansatz, qml.QNode):
            raise qml.QuantumFunctionError("Must be a callable quantum function.")

        elif isinstance(ansatz, qml.tape.QuantumTape):
            raise qml.QuantumFunctionError("Must be a callable quantum function.")

        elif callable(ansatz):
            if len(inspect.getfullargspec(ansatz).args) != 1:
                raise qml.QuantumFunctionError("Param should be a single vector")
            # user passed something that is callable but not a tape or qnode.
            try:
                if len(x0) == 1:
                    tape = qml.transforms.make_tape(ansatz)(x0[0]).expand(
                        depth=5, stop_at=lambda obj: obj.name in QiskitDevice._operation_map
                    )
                else:
                    tape = qml.transforms.make_tape(ansatz)(x0).expand(
                        depth=5, stop_at=lambda obj: obj.name in QiskitDevice._operation_map
                    )
            except IndexError:
                raise qml.QuantumFunctionError("X0 has not enough parameters")

            num_params = tape.num_params

            if len(x0) != num_params:
                x0 = 2 * np.pi * np.random.rand(num_params)

            inputs["x0"] = x0

            # raise exception if it is not a quantum function
            if len(tape.operations) == 0:
                raise qml.QuantumFunctionError("Function contains no quantum operation")

            # if no wire ordering is specified, take wire list from tape
            wires = tape.wires

            num_qubits_t = len(wires)

            if num_qubits_t > num_qubits_h:
                num_qubits = num_qubits_t
            elif num_qubits_t <= num_qubits_h:
                num_qubits = num_qubits_t

            consecutive_wires = qml.wires.Wires(range(num_qubits))
            wires_map = OrderedDict(zip(wires, consecutive_wires))

            # Create the qisit ansatz circuit
            params_vector = ParameterVector("p", num_params)

            reg = QuantumRegister(num_qubits, "q")
            circuit_ansatz = QuantumCircuit(reg, name="vqe")

            circuits = []

            j = 0

            for i, operation in enumerate(tape.operations):
                wires = qml.wires.Wires([wires_map[wire] for wire in operation.wires.tolist()])
                par = operation.parameters
                operation = operation.name
                mapped_operation = QiskitDevice._operation_map[operation]

                qregs = [reg[i] for i in wires.labels]

                if operation.split(".inv")[0] in ("QubitUnitary", "QubitStateVector"):
                    # Need to revert the order of the quantum registers used in
                    # Qiskit such that it matches the PennyLane ordering
                    qregs = list(reversed(qregs))

                dag = circuit_to_dag(QuantumCircuit(reg, name=""))
                if par:
                    par = [params_vector[j]]
                    j += 1
                gate = mapped_operation(*par)

                if operation.endswith(".inv"):
                    gate = gate.inverse()

                dag.apply_operation_back(gate, qargs=qregs)
                circuit = dag_to_circuit(dag)
                circuits.append(circuit)

            for circuit in circuits:
                circuit_ansatz &= circuit

            inputs["ansatz"] = circuit_ansatz

        else:
            raise ValueError("Input ansatz is not a tape, quantum function or a str")

    # Validate ansatz is in the module
    elif isinstance(ansatz, str):

        num_qubits = num_qubits_h

        print(num_qubits)
        ansatz_circ = getattr(lib_local, ansatz, None)
        if not ansatz_circ:
            raise ValueError("Ansatz {} not in n_local circuit library.".format(ansatz))
        inputs["ansatz"] = ansatz
        inputs["ansatz_config"] = ansatz_config

        # If given x0, validate its length against num_params in ansatz:
        x0 = np.asarray(x0)
        ansatz_circ = ansatz_circ(num_qubits, **ansatz_config)
        num_params = ansatz_circ.num_parameters

        if x0.shape[0] != num_params:
            x0 = 2 * np.pi * np.random.rand(num_params)

        inputs["x0"] = x0

    # Validate Hamiltonian

    coeff, observables = hamiltonian.terms

    if not isinstance(hamiltonian, qml.Hamiltonian):
        raise qml.QuantumFunctionError("Hamiltonian required.")

    authorized_obs = {"PauliX", "PauliY", "PauliZ", "Hadamard", "Identity"}

    for obs in observables:
        if isinstance(obs.name, list):
            for ob in obs.name:
                if ob not in authorized_obs:
                    raise qml.QuantumFunctionError("Obs not accepted")
        else:
            if obs.name not in authorized_obs:
                raise qml.QuantumFunctionError("Obs not accepted")

    # Create string Hamiltonian
    obs_str = {"PauliX": "X", "PauliY": "Y", "PauliZ": "Z", "Hadamard": "H", "Identity": "I"}

    obs_org = []
    for obs in observables:
        if isinstance(obs.name, list):
            internal = []
            for i, j in zip(obs.wires.tolist(), obs.name):
                internal.append([i, obs_str[j]])
            internal.sort()
            obs_org.append(internal)
        else:
            obs_org.append([[obs.wires.tolist()[0], obs_str[obs.name]]])

    obs_list = []
    for elem in obs_org:
        empty_obs = ["I"] * num_qubits
        for el in elem:
            empty_obs[el[0]] = el[1]
        obs_list.append(empty_obs)

    hamiltonian = []
    for i, elem in enumerate(obs_list):
        o_str = ""
        for el in elem:
            o_str += el
        hamiltonian.append((coeff[i], o_str))

    inputs["hamiltonian"] = hamiltonian

    # Set the rest of the inputs
    inputs["optimizer"] = optimizer
    inputs["optimizer_config"] = optimizer_config
    inputs["shots"] = shots
    inputs["use_measurement_mitigation"] = use_measurement_mitigation

    rt_job = RuntimeJobWrapper()
    job = provider.runtime.run(
        program_id, options=options, inputs=inputs, callback=rt_job._callback
    )
    rt_job._job = job

    return rt_job