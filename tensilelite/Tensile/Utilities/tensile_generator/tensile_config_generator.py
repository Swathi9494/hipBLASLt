import yaml
import re
import argparse
import json
import copy
import os
import subprocess
import math
import numpy as np

# Paths to the input and output files
parser = argparse.ArgumentParser(description="""Generate Tensile config file""")

parser.add_argument(
    "--hipblaslt_log",
    type=str,
    help="Path to hipblaslt log file")

parser.add_argument(
    "--tensile_config", type=str,
    help="Path to tensile config file")

parser.add_argument(
    "--gpus", type=int, default=1,
    help="Number of gpus for tuning hipblaslt")

parser.add_argument(
    "--topk", type=int, default=None,
    help="Top k gemms for tuning")

parser.add_argument(
    "--iters", type=int, default=100,
    help="Max tuning iterations")

parser.add_argument(
    "--fast", type=bool, default=False,
    help="If enabled, only tune the matrix instruction with min tile sizes, else, tune full matrix instructions")

parser.add_argument(
    "--gridbase_config", type=str, default=None,
    help="Range config path")

args = parser.parse_args()

yaml_file = args.tensile_config

NUM_WARM_UP = 20
ENQUEUES_PER_SYNC = 20
res = subprocess.run("/opt/rocm/llvm/bin/offload-arch", shell=True, capture_output=True)
ArchitectureName = res.stdout.decode('utf-8').strip()

if ArchitectureName == 'gfx942':
    res = subprocess.run("cat /sys/class/drm/card1/device/current_compute_partition", shell=True, capture_output=True)
    if res.stdout.decode('utf-8').strip() == "CPX":
        CU = 20
        XCC = 1
        GSU = [1,2,3,4,5,6,7,8]
    else:
        CU = 80
        XCC = 4
        GSU = [1,2,3,4]
    DeviceNames = ["Device 0049", "Device 0050"]
    ScheduleName = "aquavanjaram"
elif ArchitectureName == 'gfx90a':
    CU = 104
    XCC = 1
    GSU = [1,2,3,4]
    DeviceNames = ["Device 0050", "Device 0051", "Device 0052", "Device 0054", "Device 0062", "Device 7400", "Device 740c"]
    ScheduleName = "aldebaran"

fp16_instruction = [16,16,16,1]
bf16_instruction = [16,16,8,1]
tf32_instruction = [16,16,8,1]
fp32_instruction = [16,16,4,1]


HIPBLASLT_BENCH_RE = (
    r"(?P<CMD>\w+) --api_method c "
    r"-m (?P<M>[\d ]+)"
    r"-n (?P<N>[\d ]+)"
    r"-k (?P<K>[\d ]+)"
    r"--lda (?P<LDA>[\d ]+)"
    r"--ldb (?P<LDB>[\d ]+)"
    r"--ldc (?P<LDC>[\d ]+)"
    r"--ldd (?P<LDD>[\d ]+)"
    r"--stride_a (?P<STRIDE_A>[\d ]+)"
    r"--stride_b (?P<STRIDE_B>[\d ]+)"
    r"--stride_c (?P<STRIDE_C>[\d ]+)"
    r"--stride_d (?P<STRIDE_D>[\d ]+)"
    r"--alpha (?P<ALPHA>[\d\. ]+)"
    r"--beta (?P<BETA>[\d\. ]+)"
    r"--transA (?P<TRANS_A>[\w ]+)"
    r"--transB (?P<TRANS_B>[\w ]+)"
    r"--batch_count (?P<BATCH_COUNT>[\d ]+)"
    r"--a_type (?P<A_TYPE>[\w ]+)"
    r"--b_type (?P<B_TYPE>[\w ]+)"
    r"--c_type (?P<C_TYPE>[\w ]+)"
    r"--d_type (?P<D_TYPE>[\w ]+)"
    r"--scale_type (?P<SCALE_TYPE>[\w ]+)"
    r"--bias_type (?P<BIAS_TYPE>[\w ]+)"
    r"--compute_type (?P<COMPUTE_TYPE>[\w ]+)")


# Function to extract problem sizes from a line
def extract_problem_size(match):
    return [int(match.group('M').strip()), int(match.group('N').strip()), int(match.group('BATCH_COUNT').strip()), int(match.group('K').strip())]

def instruction_map(dtype_dict):
    if dtype_dict["DataType"] == 'S' and dtype_dict["F32XdlMathOp"] == 'x':
        return tf32_instruction
    elif dtype_dict["DataType"] == 'S' and dtype_dict["F32XdlMathOp"] == 0:
        return fp32_instruction
    elif dtype_dict["DataType"] == 'H':
        return fp16_instruction
    elif dtype_dict["DataType"] == 'B':
        return bf16_instruction
    else:
        return None

def datatype_map(dtype):
    if dtype == "f16_r":
        return "H"
    elif dtype == "f32_r":
        return "S"
    elif dtype == "xf32_r":
        return "XS"
    elif dtype == "bf16_r":
        return "B"
    else:
        return None
    
def trans_map(trans):
    if trans == "T":
        return True
    elif trans == "N":
        return False
    else:
        return None

def extract_dtype(match):
    DataType = datatype_map(match.group('A_TYPE').strip())
    DestDataType = datatype_map(match.group('C_TYPE').strip())
    ComputeDataType = datatype_map(match.group('COMPUTE_TYPE').strip())
    TransposeA = trans_map(match.group('TRANS_A').strip())
    TransposeB = trans_map(match.group('TRANS_B').strip())
    if DataType in ["H", "B"]:
        HighPrecisionAccumulate = True
    else:
        HighPrecisionAccumulate = False
    F32XdlMathOp = 0
    if ComputeDataType == "XS":
        ComputeDataType = "S"
        F32XdlMathOp = 'x'
    return {"Batched": True, "DataType": DataType, "DestDataType": DestDataType, "ComputeDataType": ComputeDataType, "TransposeA": TransposeA, "TransposeB": TransposeB, "HighPrecisionAccumulate": HighPrecisionAccumulate, "F32XdlMathOp": F32XdlMathOp, "OperationType": "GEMM", "UseBeta": True}

def find_matmul_instruction(mfma_instruction, size, CU):
    for bm in range(int(math.log(mfma_instruction[3],2))+1):
        for m_tiles in reversed(range(1, CU+1)):
            m_tile_size = min(size[0] // m_tiles, 256)
            wave_tile_m = math.ceil(m_tile_size / mfma_instruction[0])
            if wave_tile_m <= 0:
                continue
            for n_tiles in reversed(range(1, CU+1)):
                n_tile_size = min(size[1] // n_tiles, 256)
                wave_tile_n = math.ceil(n_tile_size / mfma_instruction[1])
                if wave_tile_n <= 0:
                    continue
                matmul_instruction = mfma_instruction + [2**bm, 1, 1, 1, 1]
                for k in reversed(range(3)):
                    if wave_tile_m // (2**k) >= 1 and wave_tile_m // (2**k) <= 32:
                        matmul_instruction[-4] = wave_tile_m // (2**k)
                        matmul_instruction[-2] = 2**k
                        
                        for l in reversed(range(3)):
                            if wave_tile_n // (2**l) >= 1 and wave_tile_n // (2**l) <= 32:
                                matmul_instruction[-3] = wave_tile_n // (2**l)
                                matmul_instruction[-1] = 2**l

                                yield matmul_instruction

def extract_range(data):
    shapes = []
    if 'Exact' in data:
        shapes += [int(shape) for shape in data['Exact'].split(',')]
    if 'Range' in data:
        shape_range = data['Range'].split(':')
        points = data['Points']
        shapes += list(set(np.round(np.linspace(int(shape_range[0]), int(shape_range[1]), int(points))).astype(int).tolist()))
    return shapes

def split_gemms_by_gpus(gemm_file, gpus):
    unique_gemms = {}
    # Read problem sizes from the input file
    with open(gemm_file, 'r') as f:
        for line in f:
            match = re.search(
                HIPBLASLT_BENCH_RE, line
            )
            if match:
                if line in unique_gemms:
                    unique_gemms[line] += 1
                else:
                    unique_gemms[line] = 1

    unique_gemms = {k: v for k, v in sorted(unique_gemms.items(), key=lambda item: item[1], reverse=True)[:args.topk]}
    for k, v in unique_gemms.items():
        print(k, v)
    unique_gemms_subgroups = [None] * gpus
    for i, (k, v) in enumerate(unique_gemms.items()):
        if unique_gemms_subgroups[i%gpus] is not None:
            unique_gemms_subgroups[i%gpus].append((k, v))
        else:
            unique_gemms_subgroups[i%gpus] = [(k, v)]
    return unique_gemms_subgroups

if args.hipblaslt_log and args.gridbase_config is None:
    unique_gemms_subgroups = split_gemms_by_gpus(args.hipblaslt_log, args.gpus)

    for gpu_idx, unique_gemms_subgroup in enumerate(unique_gemms_subgroups):
        gemm_group = {}
        matmul_instructions = {}
        if unique_gemms_subgroup is None:
            continue

        m_sum = 0
        n_sum = 0
        batch_sum = 0
        k_sum = 0
        for k, v in unique_gemms_subgroup:
            match = re.search(
                HIPBLASLT_BENCH_RE, k
            )

            if match:
                size = extract_problem_size(match)
                dtype = extract_dtype(match)
                mfma_instruction = instruction_map(dtype)
                dtype_str = json.dumps(dtype)
                if mfma_instruction is None:
                    continue
                matmul_instruction_gen = find_matmul_instruction(mfma_instruction, size, CU)
                for matmul_instruction in matmul_instruction_gen:
                    if matmul_instruction is not None:
                        if dtype_str not in matmul_instructions:
                            matmul_instructions[dtype_str] = dict()
                        matmul_instructions[dtype_str][str(matmul_instruction)] = matmul_instruction
                        if args.fast:
                            break
    
                if dtype_str in gemm_group:
                    gemm_group[dtype_str].append({'Exact': size})
                else:
                    gemm_group[dtype_str] = [{'Exact': size}]
                m_sum += size[0]
                n_sum += size[1]
                batch_sum += size[2]
                k_sum += size[3]

        m_avg = m_sum / len(unique_gemms_subgroup)
        n_avg = n_sum / len(unique_gemms_subgroup)
        batch_avg = batch_sum / len(unique_gemms_subgroup)
        k_avg = k_sum / len(unique_gemms_subgroup)

        MinFlopsPerSync = (ENQUEUES_PER_SYNC + args.iters) * m_avg * n_avg * batch_avg * k_avg / 2 
        # Read the YAML file
        with open(yaml_file, 'r') as f:
            data = yaml.safe_load(f)
        data["GlobalParameters"]["EnqueuesPerSync"] = ENQUEUES_PER_SYNC
        data["GlobalParameters"]["MaxEnqueuesPerSync"] = args.iters
        data["GlobalParameters"]["NumWarmups"] = NUM_WARM_UP
        data["GlobalParameters"]["MinFlopsPerSync"] = round(MinFlopsPerSync)

        # Update the ProblemSizes
        for i, dtype_str in enumerate(gemm_group):
            dtype = json.loads(dtype_str)

            if i>=len(data["BenchmarkProblems"]):
                data["BenchmarkProblems"].append(copy.deepcopy(data["BenchmarkProblems"][0]))
            data["BenchmarkProblems"][i][1]["BenchmarkFinalParameters"][0]["ProblemSizes"] = gemm_group[dtype_str]
            for item in data["BenchmarkProblems"][i][1]["ForkParameters"]:
                if "MatrixInstruction" in item:
                    item["MatrixInstruction"] = [list(v) for v in matmul_instructions[dtype_str].values()]
                if "WorkGroupMappingXCCGroup" in item:
                    item["WorkGroupMappingXCCGroup"] = [CU]
                if "WorkGroupMappingXCC" in item:
                    item["WorkGroupMappingXCC"] = [XCC]
                if "GlobalSplitU" in item:
                    item["GlobalSplitU"] = list(GSU)
            data["BenchmarkProblems"][i][0] = dtype
        data["LibraryLogic"]["DeviceNames"] = DeviceNames
        data["LibraryLogic"]["ScheduleName"] = ScheduleName
        data["LibraryLogic"]["ArchitectureName"] = ArchitectureName
        # Write the updated YAML file
        yaml_file = os.path.basename(yaml_file)
        with open(yaml_file.split('.')[0]+'.'+str(gpu_idx)+'.'+yaml_file.split('.')[1], 'w') as f:
            yaml.dump(data, f, default_flow_style=None)

elif args.gridbase_config and args.hipblaslt_log is None:
    unique_gemms = {}
    gpus = args.gpus

    with open(args.gridbase_config, 'r') as f:
        datas = yaml.safe_load(f)
        for data in datas:
            m_shapes = extract_range(data['M'])
            n_shapes = extract_range(data['N'])
            batch_shapes = extract_range(data['Batch'])
            k_shapes = extract_range(data['K'])
            DataType = datatype_map(data['DataType'].strip())
            DestDataType = datatype_map(data['DestDataType'].strip())
            ComputeDataType = datatype_map(data['ComputeDataType'].strip())
            TransposeA = trans_map(data['TransposeA'])
            TransposeB = trans_map(data['TransposeB'])
            if DataType in ["H", "B"]:
                HighPrecisionAccumulate = True
            else:
                HighPrecisionAccumulate = False
            F32XdlMathOp = 0
            if ComputeDataType == "XS":
                ComputeDataType = "S"
                F32XdlMathOp = 'x'
            dtype = {"Batched": True, "DataType": DataType, "DestDataType": DestDataType, "ComputeDataType": ComputeDataType, "TransposeA": TransposeA, "TransposeB": TransposeB, "HighPrecisionAccumulate": HighPrecisionAccumulate, "F32XdlMathOp": F32XdlMathOp, "OperationType": "GEMM", "UseBeta": True}
            dtype_str = json.dumps(dtype)
            for m in m_shapes:
                for n in n_shapes:
                    for batch in batch_shapes:
                        for k in k_shapes:
                            unique_gemms[(dtype_str,m,n,batch,k)] = [m,n,batch,k]

    unique_gemms_subgroups = [None] * gpus
    for i, (k, v) in enumerate(unique_gemms.items()):
        if unique_gemms_subgroups[i%gpus] is not None:
            unique_gemms_subgroups[i%gpus].append((k, v))
        else:
            unique_gemms_subgroups[i%gpus] = [(k, v)]
    for gpu_idx, unique_gemms_subgroup in enumerate(unique_gemms_subgroups):
        gemm_group = {}
        matmul_instructions = {}
        m_sum = 0
        n_sum = 0
        batch_sum = 0
        k_sum = 0
        for k, size in unique_gemms_subgroup:
            size = list(size)
            dtype_str = k[0]
            m_sum += size[0]
            n_sum += size[1]
            batch_sum += size[2]
            k_sum += size[3]
            dtype = json.loads(dtype_str)
            mfma_instruction = instruction_map(dtype)
            if mfma_instruction is None:
                continue
            matmul_instruction_gen = find_matmul_instruction(mfma_instruction, size, CU)
            for matmul_instruction in matmul_instruction_gen:
                if matmul_instruction is not None:
                    if dtype_str not in matmul_instructions:
                        matmul_instructions[dtype_str] = dict()
                    matmul_instructions[dtype_str][str(matmul_instruction)] = matmul_instruction
                    if args.fast:
                        break

            if dtype_str in gemm_group:
                gemm_group[dtype_str].append({'Exact': size})
            else:
                gemm_group[dtype_str] = [{'Exact': size}]

        m_avg = m_sum / len(unique_gemms_subgroup)
        n_avg = n_sum / len(unique_gemms_subgroup)
        batch_avg = batch_sum / len(unique_gemms_subgroup)
        k_avg = k_sum / len(unique_gemms_subgroup)

        MinFlopsPerSync = (ENQUEUES_PER_SYNC + args.iters) * m_avg * n_avg * batch_avg * k_avg / 2 
        # Read the YAML file
        with open(yaml_file, 'r') as f:
            data = yaml.safe_load(f)
        data["GlobalParameters"]["EnqueuesPerSync"] = ENQUEUES_PER_SYNC
        data["GlobalParameters"]["MaxEnqueuesPerSync"] = args.iters
        data["GlobalParameters"]["NumWarmups"] = NUM_WARM_UP
        data["GlobalParameters"]["MinFlopsPerSync"] = round(MinFlopsPerSync)

        # Update the ProblemSizes
        for i, dtype_str in enumerate(gemm_group):
            dtype = json.loads(dtype_str)

            if i>=len(data["BenchmarkProblems"]):
                data["BenchmarkProblems"].append(copy.deepcopy(data["BenchmarkProblems"][0]))
            data["BenchmarkProblems"][i][1]["BenchmarkFinalParameters"][0]["ProblemSizes"] = gemm_group[dtype_str]
            for item in data["BenchmarkProblems"][i][1]["ForkParameters"]:
                if "MatrixInstruction" in item:
                    item["MatrixInstruction"] = [list(v) for v in matmul_instructions[dtype_str].values()]
                if "WorkGroupMappingXCCGroup" in item:
                    item["WorkGroupMappingXCCGroup"] = [CU]
                if "WorkGroupMappingXCC" in item:
                    item["WorkGroupMappingXCC"] = [XCC]
                if "GlobalSplitU" in item:
                    item["GlobalSplitU"] = list(GSU)
            data["BenchmarkProblems"][i][0] = dtype
        data["LibraryLogic"]["DeviceNames"] = DeviceNames
        data["LibraryLogic"]["ScheduleName"] = ScheduleName
        data["LibraryLogic"]["ArchitectureName"] = ArchitectureName
        # Write the updated YAML file
        yaml_file = os.path.basename(yaml_file)
        with open(yaml_file.split('.')[0]+'.'+str(gpu_idx)+'.'+yaml_file.split('.')[1], 'w') as f:
            yaml.dump(data, f, default_flow_style=None)