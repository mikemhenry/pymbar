from __future__ import division  # Ensure same division behavior in py2 and py3
import logging
import numpy as np
import math
import scipy.optimize
from pymbar.utils import ensure_type, logsumexp, check_w_normalized, ParameterError
import warnings

logger = logging.getLogger(__name__)

# Below are the recommended default protocols (ordered sequence of minimization algorithms / NLE solvers) for solving the MBAR equations.
# Note: we use tuples instead of lists to avoid accidental mutability.
JAX_SOLVER_PROTOCOL = (
    dict(method="BFGS", continuation=True),
    dict(method="adaptive", options=dict(min_sc_iter=0)),
)

DEFAULT_SOLVER_PROTOCOL = (
    dict(method="hybr", continuation=True),
    dict(method="adaptive", options=dict(min_sc_iter=0)),
)

ROBUST_SOLVER_PROTOCOL = (
    dict(method="adaptive", options=dict(maxiter=1000)),
    dict(method="L-BFGS-B", options=dict(maxiter=1000)),
)

BOOTSTRAP_SOLVER_PROTOCOL = (dict(method="adaptive", options=dict(min_sc_iter=0)),)

# Allows all of the gradient based methods, but not the non-gradient methods ["Nelder-Mead", "Powell", "COBYLA"]",
scipy_minimize_options = [
    "L-BFGS-B",
    "dogleg",
    "CG",
    "BFGS",
    "Newton-CG",
    "TNC",
    "trust-ncg",
    "trust-krylov",
    "trust-exact",
    "SLSQP",
]
scipy_nohess_options = [
    "L-BFGS-B",
    "BFGS",
    "CG",
    "TNC",
    "SLSQP",
]  # don't pass a hessian to these to avoid warnings to these.
scipy_root_options = ["hybr", "lm"]  # only use root options with the hessian included


def validate_inputs(u_kn, N_k, f_k):
    """Check types and return inputs for MBAR calculations.

    Parameters
    ----------
    u_kn or q_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies or unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    u_kn or q_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies or unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='float'
        The number of samples in each state.  Converted to float because this cast is required when log is calculated.
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state
    """
    n_states, n_samples = u_kn.shape

    u_kn = ensure_type(u_kn, "float", 2, "u_kn or Q_kn", shape=(n_states, n_samples))
    N_k = ensure_type(
        N_k, "float", 1, "N_k", shape=(n_states,), warn_on_cast=False
    )  # Autocast to float because will be eventually used in float calculations.
    f_k = ensure_type(f_k, "float", 1, "f_k", shape=(n_states,))

    return u_kn, N_k, f_k


def self_consistent_update(u_kn, N_k, f_k):
    """Return an improved guess for the dimensionless free energies

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    f_k : np.ndarray, shape=(n_states), dtype='float'
        Updated estimate of f_k

    Notes
    -----
    Equation C3 in MBAR JCP paper.
    """

    u_kn, N_k, f_k = validate_inputs(u_kn, N_k, f_k)

    states_with_samples = N_k > 0

    # Only the states with samples can contribute to the denominator term.
    log_denominator_n = logsumexp(
        f_k[states_with_samples] - u_kn[states_with_samples].T, b=N_k[states_with_samples], axis=1
    )

    # All states can contribute to the numerator term.
    return -1.0 * logsumexp(-log_denominator_n - u_kn, axis=1)


def mbar_gradient(u_kn, N_k, f_k):
    """Gradient of MBAR objective function.

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    grad : np.ndarray, dtype=float, shape=(n_states)
        Gradient of mbar_objective

    Notes
    -----
    This is equation C6 in the JCP MBAR paper.
    """
    u_kn, N_k, f_k = validate_inputs(u_kn, N_k, f_k)

    log_denominator_n = logsumexp(f_k - u_kn.T, b=N_k, axis=1)
    log_numerator_k = logsumexp(-log_denominator_n - u_kn, axis=1)
    return -1 * N_k * (1.0 - np.exp(f_k + log_numerator_k))


def mbar_objective_and_gradient(u_kn, N_k, f_k):
    """Calculates both objective function and gradient for MBAR.

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state


    Returns
    -------
    obj : float
        Objective function
    grad : np.ndarray, dtype=float, shape=(n_states)
        Gradient of objective function

    Notes
    -----
    This objective function is essentially a doubly-summed partition function and is
    quite sensitive to precision loss from both overflow and underflow. For optimal
    results, u_kn can be preconditioned by subtracting out a `n` dependent
    vector.

    More optimal precision, the objective function uses math.fsum for the
    outermost sum and logsumexp for the inner sum.

    The gradient is equation C6 in the JCP MBAR paper; the objective
    function is its integral.
    """
    u_kn, N_k, f_k = validate_inputs(u_kn, N_k, f_k)

    log_denominator_n = logsumexp(f_k - u_kn.T, b=N_k, axis=1)
    log_numerator_k = logsumexp(-log_denominator_n - u_kn, axis=1)
    grad = -1 * N_k * (1.0 - np.exp(f_k + log_numerator_k))

    obj = math.fsum(log_denominator_n) - N_k.dot(f_k)

    return obj, grad


def mbar_hessian(u_kn, N_k, f_k):
    """Hessian of MBAR objective function.

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    H : np.ndarray, dtype=float, shape=(n_states, n_states)
        Hessian of mbar objective function.

    Notes
    -----
    Equation (C9) in JCP MBAR paper.
    """
    u_kn, N_k, f_k = validate_inputs(u_kn, N_k, f_k)

    W = mbar_W_nk(u_kn, N_k, f_k)

    H = W.T.dot(W)
    H *= N_k
    H *= N_k[:, np.newaxis]
    H -= np.diag(W.sum(0) * N_k)

    return -1.0 * H


def mbar_log_W_nk(u_kn, N_k, f_k):
    """Calculate the log weight matrix.

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    logW_nk : np.ndarray, dtype='float', shape=(n_samples, n_states)
        The normalized log weights.

    Notes
    -----
    Equation (9) in JCP MBAR paper.
    """
    u_kn, N_k, f_k = validate_inputs(u_kn, N_k, f_k)

    log_denominator_n = logsumexp(f_k - u_kn.T, b=N_k, axis=1)
    logW = f_k - u_kn.T - log_denominator_n[:, np.newaxis]
    return logW


def mbar_W_nk(u_kn, N_k, f_k):
    """Calculate the weight matrix.

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    W_nk : np.ndarray, dtype='float', shape=(n_samples, n_states)
        The normalized weights.

    Notes
    -----
    Equation (9) in JCP MBAR paper.
    """
    return np.exp(mbar_log_W_nk(u_kn, N_k, f_k))


def adaptive(u_kn, N_k, f_k, tol=1.0e-8, options=None):

    """
    Determine dimensionless free energies by a combination of Newton-Raphson iteration and self-consistent iteration.
    Picks whichever method gives the lowest gradient.
    Is slower than NR since it calculates the log norms twice each iteration.

    OPTIONAL ARGUMENTS
    tol (float between 0 and 1) - relative tolerance for convergence (default 1.0e-12)

    options: dictionary of options
        gamma (float between 0 and 1) - incrementor for NR iterations (default 1.0).  Usually not changed now, since adaptively switch.
        maxiter (int) - maximum number of Newton-Raphson iterations (default 10000: either NR converges or doesn't, pretty quickly)
        verbose (boolean) - verbosity level for debug output

    NOTES

    This method determines the dimensionless free energies by
    minimizing a convex function whose solution is the desired
    estimator.  The original idea came from the construction of a
    likelihood function that independently reproduced the work of
    Geyer (see [1] and Section 6 of [2]).  This can alternatively be
    formulated as a root-finding algorithm for the Z-estimator.  More
    details of this procedure will follow in a subsequent paper.  Only
    those states with nonzero counts are include in the estimation
    procedure.

    REFERENCES
    See Appendix C.2 of [1].

    """
    # put the defaults here in case we get passed an 'options' dictionary that is only partial
    options.setdefault("verbose", False)
    options.setdefault("maxiter", 10000)
    options.setdefault("print_warning", False)
    options.setdefault("gamma", 1.0)
    options.setdefault("min_sc_iter", 2)  # set a minimum number of self-consistent iterations

    gamma = options["gamma"]

    doneIterating = False
    if options["verbose"] == True:
        logger.info(
            "Determining dimensionless free energies by Newton-Raphson / self-consistent iteration."
        )

    if tol < 4.0 * np.finfo(float).eps:
        logger.info("Tolerance may be too close to machine precision to converge.")

    success = False  # fail unless solution is found.
    # keep track of Newton-Raphson and self-consistent iterations
    nr_iter = 0
    sci_iter = 0

    f_sci = np.zeros(len(f_k), dtype=np.float64)
    f_nr = np.zeros(len(f_k), dtype=np.float64)

    # Perform Newton-Raphson iterations (with sci computed on the way)

    # usually calculated at the end of the loop and saved, but we need
    # to calculate the first time.
    g = mbar_gradient(u_kn, N_k, f_k)  # Objective function gradient.

    maxiter = options["maxiter"]
    min_sc_iter = options["min_sc_iter"]

    for iteration in range(0, maxiter):

        H = mbar_hessian(u_kn, N_k, f_k)  # Objective function hessian
        Hinvg = np.linalg.lstsq(H, g, rcond=-1)[0]
        Hinvg -= Hinvg[0]
        f_nr = f_k - gamma * Hinvg

        # self-consistent iteration gradient norm and saved log sums.
        f_sci = self_consistent_update(u_kn, N_k, f_k)
        f_sci = f_sci - f_sci[0]  # zero out the minimum
        g_sci = mbar_gradient(u_kn, N_k, f_sci)
        gnorm_sci = np.dot(g_sci, g_sci)

        # newton raphson gradient norm and saved log sums.
        g_nr = mbar_gradient(u_kn, N_k, f_nr)
        gnorm_nr = np.dot(g_nr, g_nr)

        # we could save the gradient, for the next round, but it's not too expensive to
        # compute since we are doing the Hessian anyway.

        if options["verbose"]:
            logger.info(
                "self consistent iteration gradient norm is %10.5g, Newton-Raphson gradient norm is %10.5g"
                % (np.sqrt(gnorm_sci), np.sqrt(gnorm_nr))
            )
        # decide which directon to go depending on size of gradient norm
        f_old = f_k

        if gnorm_sci < gnorm_nr or sci_iter < min_sc_iter:
            f_k = f_sci
            g = g_sci
            sci_iter += 1
            if options["verbose"]:
                if sci_iter < min_sc_iter:
                    logger.info(
                        f"Choosing self-consistent iteration on iteration {iteration:d} because min_sci_iter={min_sc_iter:d}"
                    )
                else:
                    logger.info(
                        f"Choosing self-consistent iteration for lower gradient on iteration {iteration:d}"
                    )
        else:
            f_k = f_nr
            g = g_nr
            nr_iter += 1
            if options["verbose"]:
                logger.info(f"Newton-Raphson used on iteration {iteration:}")

        div = np.abs(f_k[1:])  # what we will divide by to get relative difference
        zeroed = np.abs(f_k[1:]) < np.min(
            [10 ** -8, tol]
        )  # check which values are near enough to zero, hard coded max for now.
        div[zeroed] = 1.0  # for these values, use absolute values.
        max_delta = np.max(np.abs(f_k[1:] - f_old[1:]) / div)
        max_diff = np.max(np.abs(f_sci[1:] - f_nr[1:]) / div)
        # add this just to make sure they are not too different.
        # if we start with bad states, the f_k - f_k_old might be far off.
        if np.isnan(max_delta) or ((max_delta < tol) and max_diff < np.sqrt(tol)):
            doneIterating = True
            success = True
            warn = "Convergence achieved by change in f with respect to previous guess."
            break

    if doneIterating:
        if options["verbose"]:
            logger.info(f"Converged to tolerance of {max_delta:e} in {iteration+1:d} iterations.")
            logger.info(
                f"Of {iteration+1:d} iterations, {nr_iter:d} were Newton-Raphson iterations and {sci_iter:d} were self-consistent iterations"
            )
            if np.all(f_k == 0.0):
                logger.info("WARNING: All f_k appear to be zero.")
    else:
        logger.warning("WARNING: Did not converge to within specified tolerance.")

        if maxiter <= 0:
            logger.warning(
                f"No iterations ran be cause maximum_iterations was <= 0 ({maxiter:s})!"
            )
        else:
            logger.warning(
                f"max_delta = {max_delta:e}, tol = {tol:e}, maximum_iterations = {maxiter:d}, iterations completed = {iteration:d}"
            )

    results = dict()
    results["success"] = success
    results["message"] = warn
    results["x"] = f_k

    return results


def precondition_u_kn(u_kn, N_k, f_k):
    """Subtract a sample-dependent constant from u_kn to improve precision

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state

    Returns
    -------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities

    Notes
    -----
    Returns u_kn - x_n, where x_n is based on the current estimate of f_k.
    Upon subtraction of x_n, the MBAR objective function changes by an
    additive constant, but its derivatives remain unchanged.  We choose
    x_n such that the current objective function value is zero, which
    should give maximum precision in the objective function.
    """
    u_kn, N_k, f_k = validate_inputs(u_kn, N_k, f_k)
    u_kn = u_kn - u_kn.min(0)
    u_kn += (logsumexp(f_k - u_kn.T, b=N_k, axis=1)) - N_k.dot(f_k) / float(N_k.sum())
    return u_kn


def solve_mbar_once(
    u_kn_nonzero,
    N_k_nonzero,
    f_k_nonzero,
    method="adaptive",
    tol=1e-12,
    continuation=None,
    options=None,
):
    """Solve MBAR self-consistent equations using some form of equation solver.

    Parameters
    ----------
    u_kn_nonzero : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
        for the nonempty states
    N_k_nonzero : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state for the nonempty states
    f_k_nonzero : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies for the nonempty states
    method : str, optional, default="hybr"
        The optimization routine to use.  This can be any of the methods
        available via scipy.optimize.minimize() or scipy.optimize.root().
    tol : float, optional, default=1E-14
        The convergance tolerance for minimize() or root()
    verbose: bool
        Whether to print information about the solution method.
    options: dict, optional, default=None
        Optional dictionary of algorithm-specific parameters.  See
        scipy.optimize.root or scipy.optimize.minimize for details.

    Returns
    -------
    f_k : np.ndarray
        The converged reduced free energies.
    results : dict
        Dictionary containing entire results of optimization routine, may
        be useful when debugging convergence.

    Notes
    -----
    This function requires that N_k_nonzero > 0--that is, you should have
    already dropped all the states for which you have no samples.
    Internally, this function works in a reduced coordinate system defined
    by subtracting off the first component of f_k and fixing that component
    to be zero.

    For fast but precise convergence, we recommend calling this function
    multiple times to polish the result.  `solve_mbar()` facilitates this.
    """

    f_k_nonzero = f_k_nonzero - f_k_nonzero[0]  # Work with reduced dimensions with f_k[0] := 0
    u_kn_nonzero = precondition_u_kn(u_kn_nonzero, N_k_nonzero, f_k_nonzero)

    pad = lambda x: np.pad(
        x, (1, 0), mode="constant"
    )  # Helper function inserts zero before first element
    unpad_second_arg = lambda obj, grad: (
        obj,
        grad[1:],
    )  # Helper function drops first element of gradient

    # Create objective functions / nonlinear equations to send to scipy.optimize, fixing f_0 = 0
    grad = lambda x: mbar_gradient(u_kn_nonzero, N_k_nonzero, pad(x))[
        1:
    ]  # Objective function gradient
    grad_and_obj = lambda x: unpad_second_arg(
        *mbar_objective_and_gradient(u_kn_nonzero, N_k_nonzero, pad(x))
    )  # Objective function gradient and objective function
    hess = lambda x: mbar_hessian(u_kn_nonzero, N_k_nonzero, pad(x))[1:][
        :, 1:
    ]  # Hessian of objective function

    with warnings.catch_warnings(record=True) as w:
        if method in scipy_minimize_options:
            if method in scipy_nohess_options:
                hess = None  # To suppress warning from passing a hessian function.
            results = scipy.optimize.minimize(
                grad_and_obj,
                f_k_nonzero[1:],
                jac=True,
                hess=hess,
                method=method,
                tol=tol,
                options=options,
            )
            f_k_nonzero = pad(results["x"])
        elif method == "adaptive":
            results = adaptive(u_kn_nonzero, N_k_nonzero, f_k_nonzero, tol=tol, options=options)
            f_k_nonzero = results["x"]
        elif method in scipy_root_options:
            # find the root in the gradient.
            results = scipy.optimize.root(
                grad, f_k_nonzero[1:], jac=hess, method=method, tol=tol, options=options
            )
            f_k_nonzero = pad(results["x"])
        else:
            raise ParameterError(f"Method {method} for solution of free energies not recognized")

    # If there were runtime warnings, show the messages
    if len(w) > 0:
        can_ignore = True
        for warn_msg in w:
            if "Unknown solver options" in str(warn_msg.message):
                continue
            warnings.showwarning(
                warn_msg.message,
                warn_msg.category,
                warn_msg.filename,
                warn_msg.lineno,
                warn_msg.file,
                "",
            )
            can_ignore = False  # If any warning is not just unknown options, can not skip check
        if not can_ignore:
            # Ensure MBAR solved correctly
            w_nk_check = mbar_W_nk(u_kn_nonzero, N_k_nonzero, f_k_nonzero)
            check_w_normalized(w_nk_check, N_k_nonzero)
            logger.warning(
                "MBAR weights converged within tolerance, despite the SciPy Warnings. Please validate your results."
            )

    return f_k_nonzero, results


def solve_mbar(u_kn_nonzero, N_k_nonzero, f_k_nonzero, solver_protocol=None):
    """Solve MBAR self-consistent equations using some sequence of equation solvers.

    Parameters
    ----------
    u_kn_nonzero : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
        for the nonempty states
    N_k_nonzero : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state for the nonempty states
    f_k_nonzero : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies for the nonempty states
    solver_protocol: tuple(dict()), optional, default=None
        Optional list of dictionaries of steps in solver protocol.
        If None, a default protocol will be used.

    Returns
    -------
    f_k : np.ndarray
        The converged reduced free energies.
    all_results : list(dict())
        List of results from each step of solver_protocol.  Each element in
        list contains the results dictionary from solve_mbar_once()
        for the corresponding step.

    Notes
    -----
    This function requires that N_k_nonzero > 0--that is, you should have
    already dropped all the states for which you have no samples.
    Internally, this function works in a reduced coordinate system defined
    by subtracting off the first component of f_k and fixing that component
    to be zero.

    This function calls `solve_mbar_once()` multiple times to achieve
    converged results.  Generally, a single call to solve_mbar_once()
    will not give fully converged answers because of limited numerical precision.
    Each call to `solve_mbar_once()` re-conditions the nonlinear
    equations using the current guess.
    """

    all_fks = []
    all_gnorms = []
    all_results = []

    for solver in solver_protocol:
        f_k_nonzero_result, results = solve_mbar_once(
            u_kn_nonzero, N_k_nonzero, f_k_nonzero, **solver
        )
        all_fks.append(f_k_nonzero_result)
        all_gnorms.append(
            np.linalg.norm(mbar_gradient(u_kn_nonzero, N_k_nonzero, f_k_nonzero_result))
        )
        all_results.append(results)

        if results["success"]:
            success = True
            best_gnorm = all_gnorms[-1]
            break
        else:
            logger.warning(
                f"Failed to reach a solution to within tolerance with {solver['method']}: trying next method"
            )
            logger.info(f"Ending gnorm of method {solver['method']} = {all_gnorms[-1]:e}")
            if solver["continuation"]:
                f_k_nonzero = f_k_nonzero_result
                logger.info("Will continue with results from previous method")

    if results["success"]:
        logger.info("Solution found within tolerance!")
    else:
        i_best_gnorm = np.argmin(all_gnorms)
        logger.warning("No solution found to within tolerance.")
        best_method = solver_protocol[i_best_gnorm]["method"]
        best_gnorm = all_gnorms[i_best_gnorm]
        logger.warning(
            f"The solution with the smallest gradient {best_gnorm:e} norm is {best_method}"
        )
        f_k_nonzero_result = all_fks[i_best_gnorm]
        logger.warning(
            "Please exercise caution with this solution and consider alternative methods or a different tolerance."
        )

    logger.info(f"Final gradient norm: {best_gnorm:.3g}")

    return f_k_nonzero_result, all_results


def solve_mbar_for_all_states(u_kn, N_k, f_k, solver_protocol):
    """Solve for free energies of states with samples, then calculate for
    empty states.

    Parameters
    ----------
    u_kn : np.ndarray, shape=(n_states, n_samples), dtype='float'
        The reduced potential energies, i.e. -log unnormalized probabilities
    N_k : np.ndarray, shape=(n_states), dtype='int'
        The number of samples in each state
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The reduced free energies of each state
    solver_protocol: tuple(dict()), optional, default=None
        Sequence of dictionaries of steps in solver protocol for final
        stage of refinement.

    Returns
    -------
    f_k : np.ndarray, shape=(n_states), dtype='float'
        The free energies of states
    """
    states_with_samples = np.where(N_k > 0)[0]

    if len(states_with_samples) == 1:
        f_k_nonzero = np.array([0.0])
    else:
        f_k_nonzero, all_results = solve_mbar(
            u_kn[states_with_samples],
            N_k[states_with_samples],
            f_k[states_with_samples],
            solver_protocol=solver_protocol,
        )

    f_k[states_with_samples] = f_k_nonzero

    # Update all free energies because those from states with zero samples are not correctly computed by solvers.
    f_k = self_consistent_update(u_kn, N_k, f_k)
    # This is necessary because state 0 might have had zero samples,
    # but we still want that state to be the reference with free energy 0.
    f_k -= f_k[0]

    return f_k
