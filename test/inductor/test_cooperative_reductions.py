# Owner(s): ["module: inductor"]
import torch
import torch._inductor
from torch._inductor import config
from torch._inductor.utils import run_and_get_code
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    TestCase,
)
from torch.testing._internal.inductor_utils import HAS_CUDA


@config.patch(
    {
        "triton.cooperative_reductions": True,
        "triton.force_cooperative_reductions": True,
    }
)
@instantiate_parametrized_tests
class CooperativeReductionTests(TestCase):
    def setUp(self):
        super().setUp()
        torch._inductor.metrics.generated_kernel_count = 0
        torch._dynamo.reset()

    def run_and_check(self, fn, args, *, expect_kernel_count=1):
        expected = fn(*args)
        fn = torch.compile(fn, fullgraph=True)
        result, (source_code,) = run_and_get_code(fn, *args)
        self.assertEqual(result, expected)
        self.assertIn("@triton_heuristics.cooperative_reduction", source_code)
        self.assertEqual(
            torch._inductor.metrics.generated_kernel_count, expect_kernel_count
        )
        return source_code

    @parametrize(
        "name",
        [
            "sum",
            "mean",
            "prod",
            "amin",
            "amax",
            "min",
            "max",
            "var_mean",
            "std",
            "softmax",
        ],
    )
    def test_reduction_fns(self, name):
        def fn(x, y):
            return reduction_fn(x + y, dim=-1)

        reduction_fn = getattr(torch, name)
        args = [torch.randn(1, 1024**2, device="cuda") for _ in range(2)]
        self.run_and_check(fn, args)

    def test_bool_reduction_fns(self):
        def fn(x, y):
            return [
                torch.any(x == y),
                torch.all(x == y),
                torch.any(x != y),
                torch.all(x != y),
                torch.any(x < y),
                torch.all(x > y),
            ]

        args = [torch.randn(1024, device="cuda") for _ in range(2)]
        source_code = self.run_and_check(fn, args)
        before, after = source_code.split("triton_helpers.gpu_barrier")
        self.assertEqual(before.count("if rsplit_id == ("), 0)
        self.assertEqual(after.count("if rsplit_id == ("), 6)

    @parametrize("bs", [1, 2, 5, 15])
    @parametrize("count", [1024**2 + 1, 1024**2 - 1, 1024])
    def test_non_power_of_2(self, bs, count):
        def fn(x):
            return x.mean(), x.std() + x.min()

        args = [torch.randn([bs, count], device="cuda")]
        self.run_and_check(fn, args)

    def test_chained_reductions(self):
        def fn(x):
            for _ in range(8):
                x = x + torch.softmax(x, 1)
            return x

        args = [torch.randn(4, 100000, device="cuda")]
        source_code = self.run_and_check(fn, args)
        self.assertEqual(source_code.count("triton_helpers.gpu_barrier"), 16)
        self.assertEqual(source_code.count("empty_strided_cuda"), 8)

    def test_reduce_split(self):
        def fn(a, b):
            a1 = torch.linalg.vector_norm(a)
            b1 = torch.sum(b, dim=0)
            return a1, b1

        inps = [
            torch.rand(2048, 512, device="cuda"),
            torch.rand(20, 20, device="cuda"),
        ]
        self.run_and_check(fn, inps, expect_kernel_count=2)


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if HAS_CUDA:
        run_tests(needs="filelock")
