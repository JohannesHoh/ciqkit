"""_compat.tree_order: the Windows kill walk must not follow a reused-pid link."""
from ciqkit._compat import tree_order


def _order(procs, roots, born):
    return tree_order(procs, roots, born.get)


def test_descendants_born_after_their_parent_are_followed():
    procs = [(10, 1), (11, 10), (12, 11), (99, 5)]
    born = {10: 100, 11: 110, 12: 120, 99: 50, 1: 1, 5: 1}
    assert _order(procs, [10], born) == [10, 11, 12]


def test_a_bystander_naming_a_reused_pid_as_parent_is_not_followed():
    # pid 10 (the build's bash) reuses the pid of the dead creator of the broker
    # (pid 77, born long before). Toolhelp still says 77's parent is 10.
    procs = [(77, 10), (11, 10)]
    born = {10: 500, 77: 100, 11: 510}
    assert _order(procs, [10], born) == [10, 11]


def test_unknown_creation_time_breaks_the_link():
    procs = [(11, 10), (12, 10)]
    assert _order(procs, [10], {10: 1, 11: 2}) == [10, 11]
    assert _order(procs, [10], {11: 2, 12: 3}) == [10]


def test_cycles_and_the_idle_process_terminate():
    procs = [(0, 0), (10, 11), (11, 10)]
    born = {10: 1, 11: 1, 0: 0}
    assert sorted(_order(procs, [10], born)) == [10, 11]
