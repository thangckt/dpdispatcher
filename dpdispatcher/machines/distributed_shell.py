from dpdispatcher.dlog import dlog
from dpdispatcher.machine import Machine
from dpdispatcher.utils.job_status import JobStatus
from dpdispatcher.utils.utils import (
    customized_script_header_template,
    run_cmd_with_all_output,
)

shell_script_header_template = """
#!/bin/bash -l
set -x
"""

script_env_template = """
{module_unload_part}
{module_load_part}
{source_files_part}
{export_envs_part}
{prepend_script_part}

REMOTE_ROOT=`pwd`
echo 0 > {flag_if_job_task_fail}
test $? -ne 0 && exit 1

if ! ls {submission_hash}_upload.tgz 1>/dev/null 2>&1; then
    hadoop fs -get {remote_root}/*.tgz .
fi
for TGZ in `ls *.tgz`; do tar xvf $TGZ; done

"""
script_end_template = """
cd $REMOTE_ROOT
test $? -ne 0 && exit 1

wait
FLAG_IF_JOB_TASK_FAIL=$(cat {flag_if_job_task_fail})
if test $FLAG_IF_JOB_TASK_FAIL -eq 0; then
    tar czf {submission_hash}_{job_hash}_download.tar.gz {all_task_dirs}
    hadoop fs -put -f {submission_hash}_{job_hash}_download.tar.gz {remote_root}
    hadoop fs -touchz {remote_root}/{job_tag_finished}
else
    exit 1
fi
{append_script_part}
"""


class DistributedShell(Machine):
    def gen_script_env(self, job):
        source_files_part = ""

        module_unload_part = ""
        module_purge = job.resources.module_purge
        if module_purge:
            module_unload_part += "module purge\n"
        module_unload_list = job.resources.module_unload_list
        for ii in module_unload_list:
            module_unload_part += f"module unload {ii}\n"

        module_load_part = ""
        module_list = job.resources.module_list
        for ii in module_list:
            module_load_part += f"module load {ii}\n"

        source_list = job.resources.source_list
        for ii in source_list:
            line = f"{{ source {ii}; }} \n"
            source_files_part += line

        export_envs_part = ""
        envs = job.resources.envs
        for k, v in envs.items():
            if isinstance(v, list):
                for each_value in v:
                    export_envs_part += f"export {k}={each_value}\n"
            else:
                export_envs_part += f"export {k}={v}\n"

        prepend_script = job.resources.prepend_script
        prepend_script_part = "\n".join(prepend_script)

        flag_if_job_task_fail = job.job_hash + "_flag_if_job_task_fail"

        script_env = script_env_template.format(
            flag_if_job_task_fail=flag_if_job_task_fail,
            module_unload_part=module_unload_part,
            module_load_part=module_load_part,
            source_files_part=source_files_part,
            export_envs_part=export_envs_part,
            prepend_script_part=prepend_script_part,
            remote_root=self.context.remote_root,
            submission_hash=self.context.submission.submission_hash,
        )
        return script_env

    def gen_script_end(self, job):
        all_task_dirs = ""
        for task in job.job_task_list:
            all_task_dirs += f"{task.task_work_path} "
        job_tag_finished = job.job_hash + "_job_tag_finished"
        flag_if_job_task_fail = job.job_hash + "_flag_if_job_task_fail"

        append_script = job.resources.append_script
        append_script_part = "\n".join(append_script)

        script_end = script_end_template.format(
            job_tag_finished=job_tag_finished,
            flag_if_job_task_fail=flag_if_job_task_fail,
            all_task_dirs=all_task_dirs,
            append_script_part=append_script_part,
            remote_root=self.context.remote_root,
            submission_hash=self.context.submission.submission_hash,
            job_hash=job.job_hash,
        )
        return script_end

    def gen_script_header(self, job):
        resources = job.resources
        if (
            resources["strategy"].get("customized_script_header_template_file")
            is not None
        ):
            shell_script_header = customized_script_header_template(
                resources["strategy"]["customized_script_header_template_file"],
                resources,
            )
        else:
            shell_script_header = shell_script_header_template
        return shell_script_header

    def do_submit(self, job):
        """Submit th job to yarn using distributed shell.

        Parameters
        ----------
        job : Job class instance
            job to be submitted

        Returns
        -------
        job_id: string
            submit process id
        """
        script_str = self.gen_script(job)
        script_file_name = job.script_file_name
        job_id_name = job.job_hash + "_job_id"
        output_name = job.job_hash + ".out"
        self.context.write_file(fname=script_file_name, write_str=script_str)
        script_run_str = self.gen_script_command(job)
        script_run_file_name = f"{job.script_file_name}.run"
        self.context.write_file(fname=script_run_file_name, write_str=script_run_str)

        resources = job.resources
        submit_command = (
            "hadoop jar {}/hadoop-yarn-applications-distributedshell-*.jar "
            "org.apache.hadoop.yarn.applications.distributedshell.Client "
            "-jar {}/hadoop-yarn-applications-distributedshell-*.jar "
            '-queue {} -appname "distributedshell_dpgen_{}" '
            "-shell_env YARN_CONTAINER_RUNTIME_TYPE=docker "
            "-shell_env YARN_CONTAINER_RUNTIME_DOCKER_IMAGE={} "
            "-shell_env ENV_DOCKER_CONTAINER_SHM_SIZE='600m' "
            "-master_memory 1024 -master_vcores 2 -num_containers 1 "
            "-container_resources memory-mb={},vcores={} "
            "-shell_script /tmp/{}".format(
                resources.kwargs.get("yarn_path", ""),
                resources.kwargs.get("yarn_path", ""),
                resources.queue_name,
                job.job_hash,
                resources.kwargs.get("img_name", ""),
                resources.kwargs.get("mem_limit", 1) * 1024,
                resources.cpu_per_node,
                script_file_name,
            )
        )

        cmd = (
            f"{{ nohup {submit_command} 1>{output_name} 2>{output_name} & }} && echo $!"
        )
        ret, stdout, stderr = run_cmd_with_all_output(cmd)

        if ret != 0:
            err_str = stderr.decode("utf-8")
            raise RuntimeError(
                f"Command {cmd} fails to execute, error message:{err_str}\nreturn code {ret}\n"
            )
        job_id = int(stdout.decode("utf-8").strip())

        self.context.write_file(job_id_name, str(job_id))
        return job_id

    def check_status(self, job):
        job_id = job.job_id
        if job_id == "":
            return JobStatus.unsubmitted

        ret, stdout, stderr = run_cmd_with_all_output(
            f"if ps -p {job_id} > /dev/null; then echo 1; fi"
        )
        if ret != 0:
            err_str = stderr.decode("utf-8")
            raise RuntimeError(
                f"Command fails to execute, error message:{err_str}\nreturn code {ret}\n"
            )

        if_job_exists = bool(stdout.decode("utf-8").strip())
        if self.check_finish_tag(job=job):
            dlog.info(f"job: {job.job_hash} {job.job_id} finished")
            return JobStatus.finished

        if if_job_exists:
            return JobStatus.running
        else:
            return JobStatus.terminated

    def check_finish_tag(self, job):
        job_tag_finished = job.job_hash + "_job_tag_finished"
        return self.context.check_file_exists(job_tag_finished)
