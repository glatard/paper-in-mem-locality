from nipype import Workflow, MapNode, Node, Function
from numpy import iinfo
from random import seed, sample
from time import time
from socket import gethostname
from math import ceil
import argparse
import os
import glob
import uuid
from benchmark import write_bench
try:
    from threading import get_ident
except Exception as e:
    from thread import get_ident


def get_nearest_centroid(img, centroids):
    import nibabel as nib
    import pickle
    from json import dump
    from os import path as op

    data = nib.load(img).get_data().flatten()

    assignments = {}

    for vox in data:
        distance = None
        nearest_c = None
        nearest_cv = None

        for c in centroids:
            c_dist = abs(vox - c[1])

            if (distance is None or c_dist < distance
                    or (c_dist == distance
                        and ((vox % 2 == 1 and nearest_cv < c[1])
                             or (vox % 2 == 0 and nearest_cv > c[1])))):
                distance = c_dist
                nearest_c = c[0]
                nearest_cv = c[1]

        vox = str(vox)

        if nearest_c not in assignments:
            assignments[nearest_c] = { vox: 1 }
        elif vox not in assignments[nearest_c]:
            assignments[nearest_c][vox] = 1
        else:
            assignments[nearest_c][vox] += 1

    outfiles = []

    try:
        for k, v in assignments.items():
            out_name = 'centroid-{}.json'.format(k)
            with open(out_name, 'a+') as f:
                dump(v, f)

            outfiles.append((k, op.abspath(out_name)))

    except Exception as e:
        for k, v in assignments.iteritems():
            out_name = 'centroid-{}.json'.format(k)

            with open(out_name, 'a+') as f:
                dump(v, f)

            outfiles.append((k, op.abspath(out_name)))

    return outfiles

def reduceFilesByCentroid(centroid, assignments):
    from json import load, dump
    from collections import Counter
    from os.path import basename, abspath, isfile

    try:
        a_files = [t[1] for l in assignments for t in l if t[0] == centroid[0]]

    except Exception as e:
        a_files = [l[1] for l in assignments if l[0] == centroid[0]]

    c_assignments = Counter({})
    for fn in a_files:
        if isfile(fn):
            with open(fn, 'r') as f:
                partial_a = Counter(load(f))
                c_assignments = c_assignments + partial_a

    out_name = 'centroid-{}.json'.format(centroid[0]) 
    with open(out_name, 'a+') as f:
        dump(c_assignments, f)

    return (centroid[0], abspath(out_name))


def nearest_centroid_wf(partition, centroids, work_dir, benchmark_dir=None, 
                        tmpfs='/dev/shm'):
    from nipype import Workflow, Node, MapNode, Function
    import nipype_kmeans as nk
    import uuid
    from time import time
    from benchmark import write_bench
    from socket import gethostname
    from os.path import basename, join
    try:
        from threading import get_ident
    except Exception as e:
        from thread import get_ident

    start = time()

    exec_id = uuid.uuid1()
    wf = Workflow('km_bb{}'.format(exec_id))
    wf.base_dir = (join(tmpfs, basename(work_dir))
                   if tmpfs is not None
                   else work_dir)

    gc_nname = 'gc'
    idx = 0

    for chunk in partition:
        gc_nname_it = '{0}-{1}'.format(gc_nname, idx)
        gc = Node(Function(input_names=['img', 'centroids'],
                           output_names=['assignment_files'],
                           function=nk.get_nearest_centroid),
                  name=gc_nname_it)

        gc.inputs.img = chunk
        gc.inputs.centroids = centroids

        wf.add_nodes([gc])
        idx += 1

    wf_out = wf.run('MultiProc')

    node_names = [i.name for i in wf_out.nodes()]
    result_dict = dict(zip(node_names, wf_out.nodes()))

    assignments = ([result_dict['{0}-{1}'.format(gc_nname, i)]
                                         .result
                                         .outputs
                                         .assignment_files
                    for i in range(0, len(partition))])

    assignments = [t for l in assignments for t in l]

    wf = Workflow('km_lr{}'.format(exec_id))
    wf.base_dir = work_dir

    lr_nname = 'lr'

    for c in centroids:
        lr_nname_it = '{0}-{1}'.format(lr_nname, c[0])
        lr = Node(Function(input_names=['centroid', 'assignments'],
                              output_names=['assignment_files'],
                              function=nk.reduceFilesByCentroid),
                     name=lr_nname_it)
        lr.inputs.centroid = c
        lr.inputs.assignments = assignments

        wf.add_nodes([lr])

    wf_out = wf.run('MultiProc')

    node_names = [i.name for i in wf_out.nodes()]
    result_dict = dict(zip(node_names, wf_out.nodes()))

    assignments = ([result_dict['{0}-{1}'.format(lr_nname, c[0])]
                                         .result
                                         .outputs
                                         .assignment_files
                    for c in centroids])

    end = time()

    if benchmark_dir is not None:
        write_bench('get_nearest_centroid', start, end, gethostname(),          
                    'partition', get_ident(), benchmark_dir)
    return assignments

def save_classified_wf(partition, assignments, work_dir, output_dir,
                       iteration, benchmark_dir=None):

    from nipype import Workflow, Node, Function
    import nipype_kmeans as nk
    from time import time
    from benchmark import write_bench
    from socket import gethostname
    try:
        from threading import get_ident
    except Exception as e:
        from thread import get_ident
   
    start = time()

    res_wf = Workflow('km_classify')
    res_wf.base_dir = work_dir
    c_idx = 0
    for chunk in partition:
        cc = Node(Function(input_names=['img', 'assignments', 'out_dir'],
                           output_names=['out_file'],
                           function=nk.classify_chunks),
                  name='{0}cc_{1}'.format(iteration, c_idx))

        cc.inputs.img = chunk
        cc.inputs.assignments = assignments 
        cc.inputs.out_dir = output_dir
        res_wf.add_nodes([cc])

        c_idx += 1

    res_wf.run(plugin='MultiProc')

    end = time()
    if benchmark_dir is not None:
        write_bench('save_classified', start, end, gethostname(),          
                    'partition', get_ident(), benchmark_dir)

    return ('Success', partition)

def update_centroids(centroid, assignments, benchmark_dir=None):
    import pickle
    from json import load
    from time import time
    from benchmark import write_bench
    from socket import gethostname
    try:
        from threading import get_ident
    except Exception as e:
        from thread import get_ident

    sum_elements = 0
    num_elements = 0

    with open(assignments[1], 'r') as f:
        elements = load(f)
        sum_elements += sum([float(i) * elements[i] for i in elements])
        num_elements += sum([elements[i] for i in elements])

    end = time()

    if benchmark_dir is not None:
        write_bench('update_centroids', start, gethostname(),          
                    'centroid', get_ident(), benchmark_dir)

    if sum_elements == num_elements == 0:
        return centroid
    else:
        return (centroid[0], sum_elements/num_elements)


def classify_chunks(img, assignments, out_dir, benchmark_dir=None):
    import nibabel as nib
    import pickle
    from json import load
    from os import path as op
    from numpy import where, isin

    # assume all assignment files fit in memory
    i = nib.load(img)
    data = i.get_data()
    shape = i.shape

    a = {}

    # should only be one file per centroid
    for t in assignments:
        with open(t[1], 'r') as f:
            a[t[0]] = [k for k in load(f)]

    for k in a:
        data[where(isin(data, a[k]))] = k

    i_out = nib.Nifti1Image(data, i.affine)
    i_name = op.join(out_dir, 'classified-{}'.format(op.basename(img)))
    nib.save(i_out, i_name)

    return op.abspath(i_name)


def main():
    parser = argparse.ArgumentParser(description='BigBrain K-means')
    parser.add_argument('bb_dir', type=str, help='The folder containing '
                        'BigBrain NIfTI images (local fs only)')
    parser.add_argument('iters', type=int, help='The number of iterations')
    parser.add_argument('centroids', type=float, nargs='+',
                        help="cluster centroids")
    parser.add_argument('output_dir', type=str, help='the folder to save '
                        'the final centroids to (local fs only)')
    parser.add_argument('--plugin_args', type=str,
                        help='Plugin configuration file')
    parser.add_argument('--nodes', type=int, help='Number of nodes to use')
    parser.add_argument('--benchmark', action='store_true',
                        help='benchmark pipeline')

    args = parser.parse_args()

    start = time()
    output_dir = os.path.abspath(args.output_dir)

    try:
        os.makedirs(output_dir)
    except Exception as e:
        pass

    benchmark_dir = None
    app_uuid = str(uuid.uuid1())
    
    if args.benchmark:
        benchmark_dir = os.path.abspath(os.path.join(args.output_dir,
                                                        'benchmarks-{}'.format(
                                                                    app_uuid)))
        try:
            os.makedirs(benchmark_dir)
        except Exception as e:
            pass

    # get all files in directory
    bb_files = glob.glob(os.path.join(os.path.abspath(args.bb_dir), '*'))
    dtype = iinfo('uint16')

    centroids = list(zip(range(0, len(args.centroids)), args.centroids))

    c_changed = True

    idx = 0
    result_dict = {}

    work_dir = os.path.join(os.getcwd(), 'np_km_work')

    f_per_n = ceil(len(bb_files) / args.nodes)
    file_partitions = [bb_files[x:x+f_per_n] for x in range(
                                                      0,
                                                      len(bb_files),
                                                      f_per_n)]

    while c_changed and idx < args.iters:
        wf = Workflow('km_bb1_slurm_{}'.format(idx))
        wf.base_dir = work_dir

        gc_nname = 'gc_slurm_part{}'.format(idx)
        pidx = 0
        for fp in file_partitions:

            gc_nname_it = '{0}-{1}'.format(gc_nname, pidx)
            gc = Node(Function(input_names=['partition', 'centroids',
                                            'work_dir', 'benchmark_dir'],
                                  output_names=['assignment_files'],
                                  function=nearest_centroid_wf),
                         name=gc_nname_it)

            gc.inputs.partition = fp
            gc.inputs.centroids = centroids
            gc.inputs.work_dir = work_dir
            gc.inputs.benchmark_dir = benchmark_dir

            wf.add_nodes([gc])
            pidx += 1

        if args.plugin_args is not None:
            wf_out = wf.run(plugin='SLURM',
                            plugin_args={'template': args.plugin_args})
        else:
            wf_out = wf.run(plugin='SLURM')

        # Convert to dictionary to more easily extract results
        node_names = [i.name for i in wf_out.nodes()]
        result_dict = dict(zip(node_names, wf_out.nodes()))
        assignments = ([result_dict['{0}-{1}'.format(gc_nname, i)]
                               .result
                               .outputs
                               .assignment_files 
                       for i in range(0, len(file_partitions))])

        gr_nname = 'gr_{}'.format(idx)
        uc_nname = 'uc_{}'.format(idx)

        wf = Workflow('km_bb2_slurm_{}'.format(idx))
        wf.base_dir = work_dir
        for c in centroids:
            gr_nname_it = '{0}-{1}'.format(gr_nname, c[0])
            gr = Node(Function(input_names=['centroid', 'assignments'],
                               output_names=['assignment_files'],
                               function=reduceFilesByCentroid),
                      name=gr_nname_it)
            gr.inputs.centroid = c
            gr.inputs.assignments = assignments

            wf.add_nodes([gr])

            uc_nname_it = '{0}-{1}'.format(uc_nname, c[0]) 
            uc = Node(Function(input_names=['centroid', 'assignments'],
                               output_names=['updated_centroid'],
                               function=update_centroids),
                      name=uc_nname_it)

            uc.inputs.centroid = c

            wf.connect([(gr, uc, [('assignment_files', 'assignments')])])

        if args.plugin_args is not None:
            wf_out = wf.run(plugin='SLURM',
                            plugin_args={'template': args.plugin_args})
        else:
            wf_out = wf.run(plugin='SLURM')

        # Convert to dictionary to more easily extract results
        node_names = [i.name for i in wf_out.nodes()]
        result_dict = dict(zip(node_names, wf_out.nodes()))

        new_centroids = ([result_dict['{0}-{1}'.format(uc_nname, c[0])]
                                    .result
                                    .outputs
                                    .updated_centroid
                          for c in centroids])

        old_centroids = set(centroids)
        diff = [x for x in new_centroids if x not in old_centroids]
        c_changed = bool(diff)
        centroids = new_centroids

        c_vals = [i[1] for i in centroids]
        idx += 1

        if c_changed and idx < args.iters:
            print("it", idx, c_vals)
        else:
            print("***FINAL CENTROIDS***:", idx ,c_vals)

    res_wf = Workflow('km_classify_slurm')
    res_wf.base_dir = work_dir 
    c_idx = 0

    assignments = ([result_dict['{0}-{1}'.format(gr_nname, c[0])]
                               .result
                               .outputs
                               .assignment_files
                    for c in centroids])

    for partition in file_partitions:
        cc = Node(Function(input_names=['partition', 'assignments',
                                        'work_dir', 'output_dir',
                                        'iteration', 'benchmark_dir'],
                           output_names=['results'],
                           function=save_classified_wf),
                  name='scf_{}'.format(c_idx))
        cc.inputs.partition = partition
        cc.inputs.assignments = assignments
        cc.inputs.work_dir = work_dir
        cc.inputs.output_dir = output_dir
        cc.inputs.iteration = c_idx
        cc.inputs.benchmark_dir = benchmark_dir
        res_wf.add_nodes([cc])
        c_idx += 1

    if args.plugin_args is not None:
        res_wf.run(plugin='SLURM',
                   plugin_args={ 'template': args.plugin_args})
    else:
        res_wf.run(plugin='SLURM')

    end = time()
    if benchmark_dir is not None:
        write_bench('driver_program', start, end, gethostname(),          
                    'allfiles', get_ident(), benchmark_dir)


if __name__ == '__main__':
    main()
