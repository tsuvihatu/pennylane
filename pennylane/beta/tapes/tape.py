# Copyright 2018-2020 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains the base quantum tape.
"""

from collections import deque, Sequence
import numpy as np

import pennylane as qml

from pennylane.beta.queuing import MeasurementProcess
from pennylane.beta.queuing import AnnotatedQueue, Queue, QueuingContext

from .circuit_graph import CircuitGraph


ORIGINAL_QUEUE = qml.operation.Operation.queue


def mock_queue(self):
    """Mock queuing method. When within the QuantumTape
    context, PennyLane operations are monkeypatched to
    use this queuing method rather than their built-in queuing
    method."""
    QueuingContext.append(self)
    return self


class QuantumTape(AnnotatedQueue):
    """A quantum tape recorder, that records, validates, executes,
    and differentiates variational quantum programs.

    .. note::

        As the quantum tape is a *beta* feature, the standard PennyLane
        measurement functions cannot be used. You will need to instead
        import modified measurement functions within the quantum tape:

        >>> from pennylane.beta.queuing import expval, var, sample, probs

    **Example**

    .. code-block:: python

        from pennylane.beta.tapes import QuantumTape
        from pennylane.beta.queuing import expval, var, sample, probs

        with QuantumTape() as tape:
            qml.RX(0.432, wires=0)
            qml.RY(0.543, wires=0)
            qml.CNOT(wires=[0, 'a'])
            qml.RX(0.133, wires='a')
            expval(qml.PauliZ(wires=[0]))

    Once constructed, information about the quantum circuit can be queried:

    >>> tape.operations
    [RX(0.432, wires=[0]), RY(0.543, wires=[0]), CNOT(wires=[0, 'a']), RX(0.133, wires=['a'])]
    >>> tape.observables
    [expval(PauliZ(wires=[0]))]
    >>> tape.get_parameters()
    [0.432, 0.543, 0.133]
    >>> tape.wires
    <Wires = [0, 'a']>
    >>> tape.num_params
    3

    The :class:`~.beta.tapes.CircuitGraph` can also be accessed:

    >>> tape.graph
    <pennylane.beta.tapes.circuit_graph.CircuitGraph object at 0x7fcc0433a690>

    Once constructed, the quantum tape can be executed directly on a supported
    device:

    >>> dev = qml.device("default.qubit", wires=[0, 'a'])

    Execution can take place either using the in-place constructed parameters,
    >>> tape.execute(dev)
    [0.77750694]

    or by providing parameters at run time:

    >>> tape.execute(dev, params=[0.1, 0.1, 0.1])
    [0.99003329]

    The Jacobian can also be computed using finite difference:

    >>> tape.jacobian(dev)
    [[-0.35846484 -0.46923704  0.        ]]
    >>> tape.jacobian(dev, params=[0.1, 0.1, 0.1])
    [[-0.09933471 -0.09933471  0.        ]]

    Finally, the trainable parameters can be explicitly set, and the values of
    the parameters modified in-place:

    >>> tape.trainable_params = {0} # set only the first parameter as free
    >>> tape.set_parameters(0.56)
    >>> tape.get_parameters()
    [0.56]
    >>> tape.get_parameters(free_only=False)
    [0.56, 0.543, 0.133]

    Trainable parameters are taken into account when calculating the Jacobian,
    avoiding unnecessary calculations:
    >>> tape.jacobian(dev)
    [[-0.45478169]]
    """
    cast = staticmethod(np.array)

    def __init__(self):
        super().__init__()
        self._prep = []
        self._ops = []
        self._obs = []

        self._par_info = {}
        self._trainable_params = set()
        self._graph = None
        self._output_dim = 0

        self.hash = 0
        self.is_sampled = False

    def __enter__(self):
        # monkeypatch operations to use the qml.beta.queuing.queuing.QueuingContext instead
        qml.operation.Operation.queue = mock_queue

        QueuingContext.append(self)

        return super().__enter__()

    def __exit__(self, exception_type, exception_value, traceback):
        super().__exit__(exception_type, exception_value, traceback)
        # remove the monkeypatching
        qml.operation.Operation.queue = ORIGINAL_QUEUE
        self._construct()

    # ========================================================
    # construction methods
    # ========================================================

    def _construct(self):
        """Process the annotated queue, creating a list of quantum
        operations and measurement processes.

        This method sets the following attributes:

        * ``_ops``
        * ``_obs``
        * ``_par_info``
        * ``_output_dim``
        * ``_trainable_params``
        * ``is_sampled``
        """
        param_count = 0
        op_count = 0

        for obj, info in self._queue.items():
            if not info and isinstance(obj, qml.operation.Operation):
                self._ops.append(obj)

                for p in range(len(obj.data)):
                    self._par_info[param_count] = {"op": obj, "p_idx": p}
                    param_count += 1

                op_count += 1

            if isinstance(obj, QuantumTape):
                self._ops.append(obj)

            if isinstance(obj, MeasurementProcess):
                if obj.return_type is qml.operation.Probability:
                    self._obs.append((obj, obj))
                    self._output_dim += 2 ** len(obj.wires)

                elif "owns" in info:
                    # TODO: remove the following line once devices
                    # have been refactored to no longer use obs.return_type
                    info["owns"].return_type = obj.return_type

                    self._obs.append((obj, info["owns"]))
                    self._output_dim += 1

                    if obj.return_type is qml.operation.Sample:
                        self.is_sampled = True

        self.wires = qml.wires.Wires.all_wires(
            [op.wires for op in self.operations + self.observables]
        )
        self._trainable_params = set(range(param_count))

    # ========================================================
    # properties, setters, and getters
    # ========================================================

    @property
    def trainable_params(self):
        """Store or return a set containing the indices of parameters that support
        differentiability. The indices provided match the order of appearence in the
        quantum circuit.

        Setting this property can help reduce the number of quantum evaluations needed
        to compute the Jacobian; parameters not marked as trainable will be
        automatically excluded from the Jacobian computation.

        The number of trainable parameters determines the number of parameters passed to
        :meth:`~.set_parameters`, :meth:`~.execute`, and :meth:`~.jacobian`, and changes the default
        output size of methods :meth:`~.jacobian` and :meth:`~.get_parameters()`.

        **Example**

        .. code-block:: python

            from pennylane.beta.tapes import QuantumTape
            from pennylane.beta.queuing import expval, var, sample, probs

            with QuantumTape() as tape:
                qml.RX(0.432, wires=0)
                qml.RY(0.543, wires=0)
                qml.CNOT(wires=[0, 'a'])
                qml.RX(0.133, wires='a')
                expval(qml.PauliZ(wires=[0]))

        >>> tape.trainable_params
        {0, 1, 2}
        >>> tape.trainable_params = {0} # set only the first parameter as free
        >>> tape.get_parameters()
        [0.432]

        Args:
            param_indices (set[int]): parameter indices
        """
        return self._trainable_params

    @trainable_params.setter
    def trainable_params(self, param_indices):
        """Store the indices of parameters that support differentiability.

        Args:
            param_indices (set[int]): parameter indices
        """
        if any(not isinstance(i, int) or i < 0 for i in param_indices):
            raise ValueError("Argument indices must be positive integers.")

        if any(i > self.num_params for i in param_indices):
            raise ValueError(f"Tape has at most {self.num_params} trainable parameters.")

        self._trainable_params = param_indices

    @property
    def operations(self):
        """Returns the operations on the quantum tape.

        Returns:
            list[.Operation]: list of recorded quantum operations

        **Example**

        .. code-block:: python

            from pennylane.beta.tapes import QuantumTape
            from pennylane.beta.queuing import expval, var, sample, probs

            with QuantumTape() as tape:
                qml.RX(0.432, wires=0)
                qml.RY(0.543, wires=0)
                qml.CNOT(wires=[0, 'a'])
                qml.RX(0.133, wires='a')
                expval(qml.PauliZ(wires=[0]))

        >>> tape.operations
        [RX(0.432, wires=[0]), RY(0.543, wires=[0]), CNOT(wires=[0, 'a']), RX(0.133, wires=['a'])]
        """
        return self._ops

    @property
    def observables(self):
        """Returns the observables on the quantum tape.

        Returns:
            list[.Observable]: list of recorded quantum operations

        **Example**

        .. code-block:: python

            from pennylane.beta.tapes import QuantumTape
            from pennylane.beta.queuing import expval, var, sample, probs

            with QuantumTape() as tape:
                qml.RX(0.432, wires=0)
                qml.RY(0.543, wires=0)
                qml.CNOT(wires=[0, 'a'])
                qml.RX(0.133, wires='a')
                expval(qml.PauliZ(wires=[0]))

        >>> tape.operations
        [expval(PauliZ(wires=[0]))]
        """
        return [m[1] for m in self._obs]

    @property
    def num_params(self):
        """Returns the number of trainable parameters on the quantum tape."""
        return len(self.trainable_params)

    @property
    def diagonalizing_gates(self):
        """Returns the gates that diagonalize the measured wires such that they
        are in the eigenbasis of the circuit observables.

        Returns:
            List[~.Operation]: the operations that diagonalize the observables
        """
        rotation_gates = []

        for observable in self.observables:
            rotation_gates.extend(observable.diagonalizing_gates())

        return rotation_gates

    @property
    def graph(self):
        if self._graph is None:
            self._graph = CircuitGraph(self.operations, self.observables, self.wires)

        return self._graph

    def get_parameters(self, free_only=True):
        """Return the parameters incident on the tape operations"""
        params = [o.data for o in self.operations]
        params = [item for sublist in params for item in sublist]

        if not free_only:
            return params

        return [p for idx, p in enumerate(params) if idx in self.trainable_params]

    def set_parameters(self, parameters, free_only=True):
        """Set the parameters incident on the tape operations"""
        if free_only:
            iterator = zip(self.trainable_params, parameters)
            required_length = self.num_params
        else:
            iterator = enumerate(parameters)
            required_length = len(self._par_info)

        if len(parameters) != required_length:
            raise ValueError("Number of provided parameters invalid.")

        for idx, p in iterator:
            op = self._par_info[idx]["op"]
            op.data[self._par_info[idx]["p_idx"]] = p

    # ========================================================
    # execution methods
    # ========================================================

    def execute(self, device, params=None):
        """Execute the tape on `device` with gate input `params`"""
        if params is None:
            params = self.get_parameters()

        return self.cast(self._execute(params, device=device))

    def execute_device(self, params, device):
        """Execute the tape on `device` with gate input `params`"""
        device.reset()

        # backup the current parameters
        current_parameters = self.get_parameters()

        # temporarily mutate the in-place parameters
        self.set_parameters(params)

        if isinstance(device, qml.QubitDevice):
            res = device.execute(self)
        else:
            res = device.execute(self.operations, self.observables, {})

        # restore original parameters
        self.set_parameters(current_parameters)
        return res

    _execute = execute_device

    # ========================================================
    # gradient methods
    # ========================================================

    def _grad_method(self, idx, use_graph=True):
        """Determine the correct partial derivative computation method for each gate argument.

        .. note::

            The ``QuantumTape`` only supports numerical differentiation, so
            this method will always return either ``"F"`` or ``None``. If an inheriting
            QNode supports analytic differentiation for certain operations, make sure
            that this method is overwritten appropriately to return ``"A"`` where
            required.

        Args:
            idx (int): parameter index
            use_graph: whether to use a directed-acyclic graph to determine
                if the parameter has a gradient of 0

        Returns:
            str: partial derivative method to be used
        """
        op = self._par_info[idx]["op"]

        if op.grad_method is None:
            return None

        if (self._graph is not None) or use_graph:
            # an empty list to store the 'best' partial derivative method
            # for each observable
            best = []

            # loop over all observables
            for ob in self.observables:
                # get the set of operations betweens the
                # operation and the observable
                S = self.graph.nodes_between(op, ob)

                # If there is no path between them, gradient is zero
                # Otherwise, use finite differences
                best.append("0" if not S else "F")

            if all(k == "0" for k in best):
                return "0"

        return "F"

    def igrad_numeric(self, idx, device, params=None, **options):
        """Evaluate the gradient for the ith parameter in params
        using finite differences."""
        if params is None:
            params = np.array(self.get_parameters())

        order = options.get("order", 1)
        h = options.get("h", 1e-7)

        shift = np.zeros_like(params)
        shift[idx] = h

        if order == 1:
            # forward finite-difference
            y0 = options.get("y0", np.asarray(self.execute_device(params, device)))
            y = np.array(self.execute_device(params + shift, device))
            return (y - y0) / h

        if order == 2:
            # central finite difference
            shift_forward = np.array(self.execute_device(params + shift / 2, device))
            shift_backward = np.array(self.execute_device(params - shift / 2, device))
            return (shift_forward - shift_backward) / h

        raise ValueError("Order must be 1 or 2.")

    def jacobian(self, device, params=None, method="best", **options):
        """Compute the Jacobian via the parameter-shift rule
        on `device` with gate input `params`"""
        if params is None:
            params = self.get_parameters()

        params = np.array(params)

        if options.get("order", 1) == 1:
            # the value of the circuit at current params, computed only once here
            options["y0"] = np.asarray(self.execute_device(params, device))

        jac = np.zeros((self._output_dim, len(params)), dtype=float)

        p_ind = list(np.ndindex(*params.shape))

        for idx, l in enumerate(p_ind):
            # loop through each parameter and compute the gradient
            method = self._grad_method(l[0], use_graph=options.get("use_graph", True))

            if method == "F":
                jac[:, idx] = self.igrad_numeric(l, device, params=params, **options)

        return jac