import pickle
from tests.envs import EnvFactory
import mujoco_py
import numpy as np
import Utils
import os
import matplotlib.pyplot as pp
from timeit import default_timer as timer
import matplotlib
import tests.run_opensim_simulations


# Increase font size
matplotlib.rcParams.update({'font.size': 22})


def calculate_mujoco_durations(env, data, N):

    # Open MuJoCo model and initialise a simulation
    model = mujoco_py.load_model_from_path(env.mujoco_model_file)
    sim = mujoco_py.MjSim(model)

    # Check muscle order
    Utils.check_muscle_order(model, data)

    # Get initial states
    initial_states = Utils.get_initial_states(model, env)

    # Calculate simulation duration for each run
    run_times = np.zeros((N, len(data)))
    success = np.zeros((N, len(data)))
    for run_idx in range(len(data)):

        # Get data for this run
        controls = data[run_idx]["controls"]
        timestep = data[run_idx]["timestep"]

        for repeat_idx in range(N):

            # Initialise sim
            Utils.initialise_simulation(sim, timestep, initial_states)

            # Run simulation
            start = timer()
            qpos = Utils.run_simulation(sim, controls)
            end = timer()

            # Check if simulation failed
            if np.any(np.isnan(qpos.flatten())):
                run_success = 0
            else:
                run_success = 1

            run_times[repeat_idx, run_idx] = end-start
            success[repeat_idx, run_idx] = run_success

    return run_times, success


def estimate_run_speed(env, data, train_idxs, test_idxs, output_folder):

    # Repeat each simulation N times
    N = 1

    # Calculate OpenSim simulation speed for training and test runs
    all_idxs = train_idxs + test_idxs
    opensim_durations = tests.run_opensim_simulations.run_speed_test(env, [data[idx]["run"] for idx in all_idxs], N)

    # Calculate MuJoCo simulation speed for all runs; repeat each run N times
    mujoco_durations, mujoco_success = calculate_mujoco_durations(env, data, N)

    # Check MuJoCo success rate
    print("Successful MuJoCo simulations ({} runs, {} repeats): {} %"
          .format(len(data), N, 100*mujoco_success.sum()/mujoco_success.size))

    # Compare total run time for training and test runs
    total_run_time_mujoco = mujoco_durations[:, all_idxs].mean(axis=0).mean()
    mujoco_sd = mujoco_durations[:, all_idxs].mean(axis=0).std()
    total_run_time_opensim = opensim_durations.mean(axis=0).mean()
    opensim_sd = opensim_durations.mean(axis=0).std()
    fig = pp.figure(figsize=(12, 20))
    pp.bar(np.arange(2), [total_run_time_opensim, total_run_time_mujoco], yerr=[opensim_sd, mujoco_sd])
    pp.xlabel(["OpenSim", "MuJoCo"])
    pp.ylabel("Seconds")
    pp.title("Average run time (over {} runs and {} repeats)\nOpenSim: {} seconds\nMuJoCo: {} seconds\nMuJoCo is {} times faster"
             .format(len(all_idxs), N, total_run_time_opensim, total_run_time_mujoco,
                     total_run_time_opensim/total_run_time_mujoco))
    fig.savefig(os.path.join(output_folder, 'run_time_comparison'))

    # Looks like there's one outlier in OpenSim run times, do the same analysis as above with that one removed
    mujoco_durations = mujoco_durations[:, all_idxs]
    outliers = Utils.find_outliers(np.mean(opensim_durations, axis=0), k=50) | Utils.find_outliers(np.mean(mujoco_durations, axis=0), k=50)
    total_run_time_mujoco = mujoco_durations[:, ~outliers].mean(axis=0).mean()
    mujoco_sd = mujoco_durations[:, ~outliers].mean(axis=0).std()
    total_run_time_opensim = opensim_durations[:, ~outliers].mean(axis=0).mean()
    opensim_sd = opensim_durations[:, ~outliers].mean(axis=0).std()
    fig = pp.figure(figsize=(12, 20))
    pp.bar(np.arange(2), [total_run_time_opensim, total_run_time_mujoco], yerr=[opensim_sd, mujoco_sd])
    pp.xlabel(["OpenSim", "MuJoCo"])
    pp.ylabel("Seconds")
    pp.title("Average run time (over {} runs and {} repeats)\nOpenSim: {} seconds\nMuJoCo: {} seconds\nMuJoCo is {} times faster"
             .format(sum(~outliers), N, total_run_time_opensim, total_run_time_mujoco,
                     total_run_time_opensim/total_run_time_mujoco))
    fig.savefig(os.path.join(output_folder, 'run_time_comparison_without_outliers'))


def analyse_errors(env, test_data, output_folder):

    # Get errors
    errors_default = np.stack([t["errors"]["default_parameters"] for t in test_data], axis=0)
    errors_params = np.stack([t["errors"]["optimized_parameters"] for t in test_data], axis=0)
    errors_controls = np.stack([t["errors"]["optimized_control"] for t in test_data], axis=0)

    # Do a stacked bar plot of average joint errors before and after optimization
    fig1, axs = pp.subplots(1, 2, sharey=True, figsize=(14, 8), gridspec_kw={'width_ratios': [2, 1]})
    avgs_default = np.mean(errors_default, axis=0)
    std_default = np.std(errors_default, axis=0)
    avgs_params = np.mean(errors_params, axis=0)
    std_params = np.std(errors_params, axis=0)
    avgs_controls = np.mean(errors_controls, axis=0)
    std_controls = np.std(errors_controls, axis=0)
    colors = ["tab:blue", "tab:orange", "tab:green", "tab:purple", "tab:brown", "tab:red", "tab:cyan"]
    handles = []
    for joint_idx in range(errors_default.shape[1]):

        # Calculate bottom values
        bottom_default = np.sum(avgs_default[:joint_idx])
        bottom_params = np.sum(avgs_params[:joint_idx])
        bottom_controls = np.sum(avgs_controls[:joint_idx])

        # Do bar plots
        h = axs[0].bar(0, avgs_default[joint_idx], yerr=std_default[joint_idx], width=0.25, bottom=bottom_default, color=colors[joint_idx])
        axs[0].bar(0.3, avgs_params[joint_idx], yerr=std_params[joint_idx], width=0.25, bottom=bottom_params, color=colors[joint_idx])
        handles.append(h)
        axs[1].bar(0, avgs_controls[joint_idx], yerr=std_controls[joint_idx], width=0.25, bottom=bottom_controls, color=colors[joint_idx])

    # Set labels and such
    axs[0].set_xticks([0, 0.3])
    axs[0].set_xticklabels(["default params", "optimized params"])
    axs[0].set_ylabel('Mean absolute error')
    axs[0].legend(handles, env.target_states)
    axs[1].set_xticks([0])
    axs[1].set_xticklabels(["optimized params\nand controls"])
    pp.tight_layout()
    fig1.savefig(os.path.join(output_folder, 'joint_errors_stacked_bar_plot'))

    # Do another bar plot but use separate bars for joints
    x = np.arange(len(avgs_default))
    fig2 = pp.figure(figsize=(20, 8))
    pp.bar(x-0.25, avgs_default, yerr=std_default, width=0.25)
    pp.bar(x, avgs_params, yerr=std_params, width=0.25)
    pp.bar(x+0.25, avgs_controls, yerr=std_controls, width=0.25)
    pp.xticks(x, env.target_states)
    pp.ylabel('Radians')
    pp.legend(["Default params", "Optimized params", "Optimized params + controls"])
    pp.tight_layout()
    pp.ylim(bottom=0)
    fig2.savefig(os.path.join(output_folder, 'joint_errors_bar_plot'))


def analyse_controls(env, test_data, output_folder):

    # Go through each run and calculate "control utilisation" for each muscle
    u = []
    u_opt = []
    ctrl_err = []
    for run in test_data:

        ctrl = run["controls"]
        ctrl_opt = run["optimized_controls"]

        # Calculate utilisation for actual control and optimized control
        u.append(ctrl.sum(axis=0)/ctrl.shape[0])
        u_opt.append(ctrl_opt.sum(axis=0)/ctrl.shape[0])

        # Get absolute control error
        ctrl_err.append(abs(ctrl - ctrl_opt))

    # Plot average disparity
    u = np.stack(u, axis=0)
    u_opt = np.stack(u_opt, axis=0)
    muscle_names = list(env.initial_states["actuators"])
    fig1, ax = pp.subplots(figsize=(24, 12))
    ax.boxplot(abs(u-u_opt)*100, showfliers=False, labels=muscle_names)
    pp.xticks(rotation=90)
    pp.ylim(bottom=0, top=100)
    #pp.title('Average difference in\ntotal muscle utilisation')
    pp.ylabel("Percentage points")
    pp.tick_params(axis='x', which='both', bottom=False, top=False)
    pp.tight_layout()
    fig1.savefig(os.path.join(output_folder, "difference_in_total_muscle_utilisation"))

    # Plot mean absolute control error
    ctrl_err = np.stack(ctrl_err, axis=2)
    err_per_run = np.mean(ctrl_err, axis=0)
    fig2, ax = pp.subplots(figsize=(24, 12))
    ax.boxplot(err_per_run.transpose(), showfliers=False, labels=muscle_names)
    pp.xticks(rotation=90)
    pp.ylim(bottom=0, top=1)
    #pp.title("Mean absolute error\nbetween control signals")
    pp.ylabel("Control value")
    pp.tick_params(axis='x', which='both', bottom=False, top=False)
    pp.tight_layout()
    fig2.savefig(os.path.join(output_folder, "MAE_control_signals"))


def main(model_name):

    # Load test data
    env = EnvFactory.get(model_name)
    with open(env.data_file, 'rb') as f:
        params, data, train_idxs, test_idxs = pickle.load(f)

    # Create a folder for output figures if it doesn't exist
    output_folder = os.path.join(env.output_folder, '..', 'figures')
    os.makedirs(output_folder, exist_ok=True)

    # Get test data
    test_data = [data[idx] for idx in test_idxs]

    # Analyse errors
    analyse_errors(env, test_data, output_folder)

    # Analyse controls
    analyse_controls(env, test_data, output_folder)

    # Run speed analysis
    #estimate_run_speed(env, data, train_idxs, test_idxs, output_folder)


if __name__ == "__main__":
    #main(sys.argv[1])
    main("mobl_arms")
    #main("leg6dof9musc")