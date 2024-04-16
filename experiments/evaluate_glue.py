import json
import shutil
from itertools import islice
import time
from typing import Tuple, Union
import sys
import os
from types import SimpleNamespace

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.append('/home/akshatgupta/KnowledgeEditing/model-editing') 
from baselines.ft import FTHyperParams, apply_ft_to_model
from baselines.mend import MENDHyperParams, MendRewriteExecutor
from dsets import (
    AttributeSnippets,
    CounterFactDataset,
    MENDQADataset,
    MultiCounterFactDataset,
    get_tfidf_vectorizer,
)
from experiments.py.eval_utils_counterfact import compute_rewrite_quality_counterfact
from experiments.py.eval_utils_zsre import compute_rewrite_quality_zsre
from memit import MEMITHyperParams, apply_memit_to_model
from rome import ROMEHyperParams, apply_rome_to_model
from dsets.counterfact import CounterFactDataset
from util import nethook
from util.globals import *

from glue_eval.glue_eval import GLUEEval

ALG_DICT = {
    "MEMIT": (MEMITHyperParams, apply_memit_to_model),
    "ROME": (ROMEHyperParams, apply_rome_to_model),
    "FT": (FTHyperParams, apply_ft_to_model),
    "MEND": (MENDHyperParams, MendRewriteExecutor().apply_to_model),
}

DS_DICT = {
    "mcf": (MultiCounterFactDataset, compute_rewrite_quality_counterfact),
    "cf": (CounterFactDataset, compute_rewrite_quality_counterfact),
    "zsre": (MENDQADataset, compute_rewrite_quality_zsre),
}


def main(
    args,
    alg_name: str,
    model_name: Union[str, Tuple],
    hparams_fname: str,
    ds_name: str,
    dataset_size_limit: int,
    continue_from_run: str,
    skip_generation_tests: bool,
    generation_test_interval: int,
    conserve_memory: bool,
    dir_name: str,
    num_edits: int = 1,
    use_cache: bool = False,
    model_save_location: str = None
):
    # Set algorithm-specific variables
    params_class, apply_algo = ALG_DICT[alg_name]

    # Determine run directory
    # Create new dir if not continuing from prev run OR prev run doesn't exist
    if (
        continue_from_run is None
        or not (run_dir := RESULTS_DIR / dir_name / continue_from_run).exists()
    ):
        continue_from_run = None
    if continue_from_run is None:
        alg_dir = RESULTS_DIR / dir_name
        if alg_dir.exists():
            id_list = [
                int(str(x).split("_")[-1])
                for x in alg_dir.iterdir()
                if str(x).split("_")[-1].isnumeric()
            ]
            run_id = 0 if not id_list else max(id_list) + 1
        else:
            run_id = 0
        run_dir = RESULTS_DIR / dir_name / f"run_{str(run_id).zfill(3)}"
        run_dir.mkdir(parents=True, exist_ok=True)
    else: run_id = int(continue_from_run.split("_")[-1]) # for previous runs
    print(f"Results will be stored at {run_dir}")
    print(run_id)

    # Get run hyperparameters
    params_path = (
        run_dir / "params.json"
        if continue_from_run is not None
        else HPARAMS_DIR / alg_name / hparams_fname
    )
    hparams = params_class.from_json(params_path)
    if not (run_dir / "params.json").exists():
        shutil.copyfile(params_path, run_dir / "params.json")
    print(f"Executing {alg_name} with parameters {hparams}")

    # Instantiate vanilla model
    if type(model_name) is str:
        print("Instantiating model")
        model = AutoModelForCausalLM.from_pretrained(model_name).cuda()
        original_model = AutoModelForCausalLM.from_pretrained(model_name)#.cuda()
        tok = AutoTokenizer.from_pretrained(model_name)
        tok.pad_token = tok.eos_token
    else:
        model, tok = model_name
        model_name = model.config._name_or_path

    # Load data
    print("Loading dataset, attribute snippets, tf-idf data")
    snips = AttributeSnippets(DATA_DIR) if not skip_generation_tests else None
    vec = get_tfidf_vectorizer(DATA_DIR) if not skip_generation_tests else None

    #if num_edits > 1:
    #    assert ds_name != "cf", f"{ds_name} does not support multiple edits"

    ds_class, ds_eval_method = DS_DICT[ds_name]
    ds = ds_class(DATA_DIR, tok=tok, size=dataset_size_limit)

    # Get cache templates
    cache_template = None
    if use_cache:
        cache_template = (
            KV_DIR
            / f"{model_name.replace('/', '_')}_{alg_name}"
            / f"{ds_name}_layer_{{}}_clamp_{{}}_case_{{}}.npz"
        )
        print(f"Will load cache from {cache_template}")

    #load indices file and initialize dataset class
    if ds_name == 'cf':
        indices_filename = 'counterfact_sampled_unique_10_20391.json'
        dataset = CounterFactDataset('data')
    elif ds_name == 'zsre':
        indices_filename = 'zsre_sampled_unique_10_10720.json'
        dataset = MENDQADataset('data', tok)

    f = open(indices_filename)
    sampled_indices = json.load(f)

    # Iterate through dataset
    glue_save_location = str(run_dir) + '/' + 'glue_eval/'
    os.makedirs(glue_save_location, exist_ok=True)

    edits_so_far = []
    memory_tracker = {key: [] for key in ['max_memory', 'memory']}
    for e in range(0, len(sampled_indices[args.sample_num]), num_edits):
        if e >= 1000: break # run sample on smaller dataset

        record_chunks = []
        for element_index in sampled_indices[args.sample_num][e: min(e+num_edits, len(sampled_indices[args.sample_num]))]:
            datapoint = dataset.__getitem__(element_index)
            record_chunks.append(datapoint)
            edits_so_far.append(datapoint)

        case_result_template = str(run_dir / "{}_edits-case_{}.json")
        # Is the chunk already done?
        already_finished = True
        for record in record_chunks:
            if not Path(
                case_result_template.format(num_edits, record["case_id"])
            ).exists():
                already_finished = False
                break
        if already_finished:
            continue

        # Compute weight changes + record weights that changed
        case_ids = [record["case_id"] for record in record_chunks]
        args_conserve_memory = (
            dict(return_orig_weights_device=("cpu" if conserve_memory else "cuda"))
            if conserve_memory
            else dict()
        )
        etc_args = dict(cache_template=cache_template) if any(alg in alg_name for alg in ["ROME", "MEMIT"]) else dict()

        start = time.time()
        edited_model, weights_copy = apply_algo(
            model,
            tok,
            [
                {"case_id": record["case_id"], **record["requested_rewrite"]}
                for record in record_chunks
            ],
            hparams,
            copy=False,
            return_orig_weights=True,
            **args_conserve_memory,
            **etc_args,
        )
        exec_time = time.time() - start
        print("Execution took", exec_time)

        if alg_name == 'MEND':
            mend_factors = weights_copy[1]
            weights_copy = weights_copy[0]            

            hparams_distance = {'layers':[]}
            for layer in mend_factors.keys():
                if 'transformer' in layer:
                    hparams_distance['layers'].append(layer[:-2])

            hparams_distance['layers'] = list(set(hparams_distance['layers']))
            hparams_distance = SimpleNamespace(**hparams_distance)
        else:
            hparams_distance = hparams

        ###### Evaluate new model
        start = time.time()
        gen_test_vars = [snips, vec]
        for record in record_chunks:
            out_file = Path(case_result_template.format(num_edits, record["case_id"]))
            if out_file.exists():
                print(f"Skipping {out_file}; already exists")
                continue

            metrics = {
                "case_id": record["case_id"],
                "grouped_case_ids": case_ids,
                "num_edits": num_edits,
                "requested_rewrite": record["requested_rewrite"],
                "time": exec_time,
                "post": ds_eval_method(
                    edited_model,
                    tok,
                    record,
                    *(
                        gen_test_vars
                        if record["case_id"] % generation_test_interval == 0
                        else [None, None]
                    ),  # Only test generation every generation_test_interval cases
                ),
            }

            # Dump metrics in .json
            with open(out_file, "w") as f:
                json.dump(metrics, f, indent=1)

        print("Evaluation took", time.time() - start)

        if e == 0:#do initial GLUE EVAL WITH ORIGINAL MODEL
            distance = get_model_distance(original_model, original_model, hparams_distance)
            edited_model.cpu()#off load edited model from gpu for this evaluation

            glue_results = {
                'sample_num': args.sample_num,
                'edit_num': -1,
                'element_index': -1,
                'case_id': -1,
                'distance_from_original': distance,
                }

            out_file = glue_save_location + str(-1) + '_' + "base.json"
            glue_eval = GLUEEval(original_model.cuda(), tok)
            glue_results = glue_eval.evaluate(glue_results, out_file, sst_flag = args.sst_eval, mrpc_flag = args.mrpc_eval, cola_flag=args.cola_eval, rte_flag=args.rte_eval)
            
            #off load original model from GPU, load edited model back to gpu
            original_model.cpu()
            edited_model.cuda()

            #store the individual overall result file
            output_filename = out_file.replace('.json', '_glue.json')
            with open(output_filename, "w") as f:
                json.dump(glue_results, f, indent=4)



        #####GLUE EVALUATION CODES
        if e != 0 and e % args.glue_eval_interval == 0:
            distance = get_model_distance(original_model, edited_model, hparams_distance)

            model = edited_model
            glue_results = {
                'sample_num': args.sample_num,
                'edit_num': e,
                'element_index': element_index,
                'case_id': record['case_id'],
                'distance_from_original': distance,
                }

            out_file = glue_save_location + str(e) + '_' + "case_{}.json".format(record["case_id"])
            glue_eval = GLUEEval(model, tok)
            glue_results = glue_eval.evaluate(glue_results, out_file, sst_flag = args.sst_eval, mrpc_flag = args.mrpc_eval, cola_flag=args.cola_eval, rte_flag=args.rte_eval)
            
            #store the individual overall result file
            output_filename = out_file.replace('.json', '_glue.json')
            with open(output_filename, "w") as f:
                json.dump(glue_results, f, indent=4)

        if e != 0 and e % args.model_save_interval == 0:
            if model_save_location is None:
                model_save_folder = '/data/anuragrao/edited_models/' + alg_name + '/' + f"run_{str(run_id).zfill(3)}" + '/edits_' + str(e + 1)
            else:
                print("Model storage location provided at " + model_save_location)
                model_save_folder = model_save_location + alg_name + '/' + f"run_{str(run_id).zfill(3)}" + '/edits_' + str(e + 1)
            os.makedirs(model_save_folder)
            model.save_pretrained(model_save_folder)

        memory_tracker['max_memory'].append(torch.cuda.max_memory_allocated())
        memory_tracker['memory'].append(torch.cuda.memory_allocated())

    memory_out_file = str(run_dir) + '/memory.json'
    with open(memory_out_file, 'w') as f:
        json.dump(memory_tracker, f)
    



def window(seq, n=2):
    "Returns a sliding window (of width n) over data from the iterable"
    "   s -> (s0,s1,...s[n-1]), (s1,s2,...,sn), ...                   "
    it = iter(seq)
    result = tuple(islice(it, n))
    if len(result) == n:
        yield result
    for elem in it:
        result = result[1:] + (elem,)
        yield result


def chunks(arr, n):
    """Yield successive n-sized chunks from arr."""
    for i in range(0, len(arr), n):
        yield arr[i : i + n]

def get_model_distance(original_model, model_new, model_hpar):
    state_dict_original = original_model.state_dict()
    state_dict_new = model_new.state_dict()

    distances_dict = {}
    for layer in model_hpar.layers:
        if isinstance(layer, str) and 'transformer' in layer:
            rewrite_layer = layer
        else:
            rewrite_layer = model_hpar.rewrite_module_tmp.format(str(layer)) + '.weight'

        distance = torch.norm(state_dict_original[rewrite_layer] - state_dict_new[rewrite_layer].cpu()) / state_dict_original[rewrite_layer].numel()
        distances_dict[layer] = distance.detach().cpu().item()
    
    return distances_dict


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sample_num",
        type=str,   
        default="3",
        help="Name of hyperparameters file, located in the hparams/<alg_name> folder.",
        required=False,
    )
    parser.add_argument(
        "--alg_name",
        choices=["MEMIT", "ROME", "FT", "MEND"],
        default="ROME",
        help="Editing algorithm to use. Results are saved in results/<alg_name>/<run_id>, "
        "where a new run_id is generated on each run. "
        "If continuing from previous run, specify the run_id in --continue_from_run.",
        required=False,
    )
    parser.add_argument(
        "--model_name",
        choices=["gpt2-medium", "gpt2-large", "gpt2-xl", "EleutherAI/gpt-j-6B"],
        default= "gpt2-xl",
        help="Model to edit.",
        required=False,
    )
    parser.add_argument(
        "--hparams_fname",
        type=str,
        default= "gpt2-xl.json",
        help="Name of hyperparameters file, located in the hparams/<alg_name> folder.",
        required=False,
    )
    parser.add_argument(
        "--ds_name",
        choices=["mcf", "cf", "zsre"],
        default="cf",
        help="Dataset to perform evaluations on. Either CounterFact (cf), MultiCounterFact (mcf), or zsRE (zsre).",
    )
    parser.add_argument(
        "--sst_eval",
        type=bool,
        default=True,
        help="Name of hyperparameters file, located in the hparams/<alg_name> folder.",
        required=False,
    )
    parser.add_argument(
        "--mrpc_eval",
        type=bool,
        default=True,
        help="Name of hyperparameters file, located in the hparams/<alg_name> folder.",
        required=False,
    )
    parser.add_argument(
        "--cola_eval",
        type=bool,
        default=True,
        help="Name of hyperparameters file, located in the hparams/<alg_name> folder.",
        required=False,
    )
    parser.add_argument(
        "--rte_eval",
        type=bool,
        default=True,
        help="Name of hyperparameters file, located in the hparams/<alg_name> folder.",
        required=False,
    )
    parser.add_argument(
        "--glue_eval_interval",
        type=int,
        default=5,
        help="Truncate CounterFact to first n records.",
    )
    parser.add_argument(
        "--model_save_interval",
        type=int,
        default=20,
        help="Truncate CounterFact to first n records.",
    )
    parser.add_argument(
        "--continue_from_run",
        type=str,
        default=None,
        help="If continuing from previous run, set to run_id. Otherwise, leave as None.",
    )
    parser.add_argument(
        "--dataset_size_limit",
        type=int,
        default=None,
        help="Truncate CounterFact to first n records.",
    )
    parser.add_argument(
        "--skip_generation_tests",
        dest="skip_generation_tests",
        action="store_true",
        help="Only run fast probability-based tests without slow generation tests. "
        "Useful for quick debugging and hyperparameter sweeps.",
    )
    parser.add_argument(
        "--generation_test_interval",
        type=int,
        default=1,
        help="One generation test is performed every [flag_value] iterations. If -1, generation tests are skipped.",
    )
    parser.add_argument(
        "--conserve_memory",
        dest="conserve_memory",
        action="store_true",
        help="Reduce memory usage during evaluation at the cost of a minor slowdown. "
        "Backs up model weights on CPU instead of GPU.",
    )
    parser.add_argument(
        "--num_edits",
        type=int,
        default=1,
        help="Number of rewrites to perform simultaneously.",
    )
    parser.add_argument(
        "--use_cache",
        dest="use_cache",
        action="store_true",
        help="Use cached k/v pairs",
    )
    parser.add_argument(
        "--model_save_location",
        type=str,
        default=None,
        help="Location to store edited models"
    )
    parser.set_defaults(skip_generation_tests=False, conserve_memory=False)
    args = parser.parse_args()

    main(
        args,
        args.alg_name,
        args.model_name,
        args.hparams_fname,
        args.ds_name,
        args.dataset_size_limit,
        args.continue_from_run,
        args.skip_generation_tests,
        args.generation_test_interval,
        args.conserve_memory,
        dir_name=args.alg_name,
        num_edits=args.num_edits,
        use_cache=args.use_cache,
        model_save_location=args.model_save_location
    )
