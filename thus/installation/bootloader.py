#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  bootloader.py
#
#  This file was forked from Cnchi (graphical installer from Antergos)
#  Check it at https://github.com/antergos
#
#  Copyright © 2013-2015 Antergos (http://antergos.com/)
#  Copyright © 2013-2015 Manjaro (http://manjaro.org)
#
#  This file is part of Thus.
#
#  Thus is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  Thus is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Thus; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

""" Bootloader installation """

import logging
import os
import shutil
import subprocess
import re

import parted3.fs_module as fs

from installation import chroot

import random
import string

from configobj import ConfigObj

conf_file = '/etc/thus.conf'
configuration = ConfigObj(conf_file)

# When testing, no _() is available
try:
    _("")
except NameError as err:
    def _(message):
        return message


class Bootloader(object):
    def __init__(self, dest_dir, settings, mount_devices):
        self.dest_dir = dest_dir
        self.settings = settings
        self.mount_devices = mount_devices
        self.method = settings.get("partition_mode")
        self.root_device = self.mount_devices["/"]
        self.root_uuid = fs.get_info(self.root_device)['UUID']
        self.swap_partition = self.mount_devices["swap"]
        self.swap_uuid = fs.get_info(self.swap_partition)['UUID']
        self.boot_device = self.mount_devices["/boot"]
        self.boot_uuid = fs.get_info(self.boot_device)['UUID']
        self.kernel = configuration['install']['KERNEL']
        self.vmlinuz = "vmlinuz-{0}".format(self.kernel)
        self.initramfs = "initramfs-{0}".format(self.kernel)

    def install(self):
        """ Installs the bootloader """

        # Freeze and unfreeze xfs filesystems to enable bootloader installation on xfs filesystems
        self.freeze_unfreeze_xfs()

        bootloader = self.settings.get('bootloader').lower()
        if bootloader == "grub2":
            self.install_grub()
        elif bootloader == "gummiboot":
            self.install_gummiboot()

    def install_grub(self):
        self.modify_grub_default()
        # self.prepare_grub_d()

        if os.path.exists('/sys/firmware/efi'):
            self.install_grub2_efi()
        else:
            self.install_grub2_bios()

        self.check_root_uuid_in_grub()

    def check_root_uuid_in_grub(self):
        """ Checks grub.cfg for correct root UUID """
        cfg = os.path.join(self.dest_dir, "boot/grub/grub.cfg")
        if len(self.root_uuid) == 0:
            logging.warning(_("'ruuid' variable is not set. I can't check root UUID in grub.cfg, let's hope it's ok"))
            return
        ruuid_str = 'root=UUID=' + self.root_uuid
        boot_command = self.settings.get('GRUB_CMDLINE_LINUX')
        boot_command = 'linux /' + self.vmlinuz + ' ' + ruuid_str + ' ' + boot_command + '\n'
        pattern = re.compile("menuentry 'Manjaro Linux'[\s\S]*{0}.img\n}".format(self.vmlinuz))

        with open(cfg) as grub_file:
            parse = grub_file.read()

        if not self.settings.get('use_luks') and ruuid_str not in parse:
            entry = pattern.search(parse)
            if entry:
                logging.debug("Wrong uuid in grub.cfg, let's fix it!")
                new_entry = re.sub("linux\t/{0}.*quiet\n".format(self.vmlinuz), boot_command, entry.group())
                parse = parse.replace(entry.group(), new_entry)

                with open(cfg) as grub_file:
                    grub_file.write(parse)

    def modify_grub_default(self):
        """ If using LUKS as root, we need to modify GRUB_CMDLINE_LINUX
        GRUB_CMDLINE_LINUX : Command-line arguments to add to menu entries for the Linux kernel.
        GRUB_CMDLINE_LINUX_DEFAULT : Unless ‘GRUB_DISABLE_RECOVERY’ is set to ‘true’, two menu
            entries will be generated for each Linux kernel: one default entry and one entry
            for recovery mode. This option lists command-line arguments to add only to the default
            menu entry, after those listed in ‘GRUB_CMDLINE_LINUX’. """

        plymouth_bin = os.path.join(self.dest_dir, "usr/bin/plymouth")
        if os.path.exists(plymouth_bin):
            use_splash = "splash"
        else:
            use_splash = ""

        if "swap" in self.mount_devices:
            cmd_linux_default = 'resume=UUID={0} quiet {1}'.format(self.swap_uuid, use_splash)
        else:
            cmd_linux_default = 'quiet {0}'.format(use_splash)

        self.set_grub_option("GRUB_THEME", "/boot/grub/themes/Manjaro-Default/theme.txt")
        self.set_grub_option("GRUB_CMDLINE_LINUX_DEFAULT", cmd_linux_default)
        self.set_grub_option("GRUB_DISTRIBUTOR", "Manjaro")

        if self.settings.get('use_luks'):
            # Let GRUB automatically add the kernel parameters for root encryption
            luks_root_volume = self.settings.get('luks_root_volume')

            logging.debug("Luks Root Volume: %s", luks_root_volume)

            root_device = self.root_device

            if self.method == "advanced" and self.settings.get('use_luks_in_root'):
                # Special case, in advanced when using luks in root device, we store it in luks_root_device
                root_device = self.settings.get('luks_root_device')

            root_uuid = fs.get_info(root_device)['UUID']

            logging.debug("Root device: %s", root_device)

            cmd_linux = "cryptdevice=/dev/disk/by-uuid/{0}:{1}".format(root_uuid, luks_root_volume)

            if self.settings.get("luks_root_password") == "":
                # No luks password, so user wants to use a keyfile
                cmd_linux += " cryptkey=/dev/disk/by-uuid/{0}:ext2:/.keyfile-root".format(self.boot_uuid)

            # Store grub line in settings, we'll use it later in check_root_uuid_in_grub()
            self.settings.set('GRUB_CMDLINE_LINUX', cmd_linux)
            # Store grub line in /etc/default/grub file
            self.set_grub_option("GRUB_CMDLINE_LINUX", cmd_linux)

        logging.debug(_("/etc/default/grub configuration completed successfully."))

    def set_grub_option(self, option, cmd):
        """ Changes a grub setup option in /etc/default/grub """
        try:
            default_grub = os.path.join(self.dest_dir, "etc/default", "grub")

            with open(default_grub) as grub_file:
                lines = [x.strip() for x in grub_file.readlines()]

            option_found = False

            for i in range(len(lines)):
                if option + "=" in lines[i]:
                    option_found = True
                    lines[i] = '{0}="{1}"\n'.format(option, cmd)

            if option_found:
                # Option was found and changed, store our changes
                with open(default_grub, 'w') as grub_file:
                    grub_file.write("\n".join(lines) + "\n")
            else:
                # Option was not found. Thus, append new option
                with open(default_grub, 'a') as grub_file:
                    grub_file.write('{0}="{1}"\n'.format(option, cmd))

            logging.debug('Set %s="%s" in /etc/default/grub', option, cmd)
        except Exception as general_error:
            logging.error("Can't modify /etc/default/grub")
            logging.error(general_error)

    def prepare_grub_d(self):
        """ Copies 10_manjaro script into /etc/grub.d/ """
        grub_d_dir = os.path.join(self.dest_dir, "etc/grub.d")
        script_dir = os.path.join(self.settings.get("thus"), "scripts")
        script = "10_manjaro"

        if not os.path.exists(grub_d_dir):
            os.makedirs(grub_d_dir)

        script_path = os.path.join(script_dir, script)
        if os.path.exists(script_path):
            try:
                shutil.copy2(script_path, grub_d_dir)
                os.chmod(os.path.join(grub_d_dir, script), 0o755)
            except FileNotFoundError:
                logging.debug(_("Could not copy %s to grub.d"), script)
            except FileExistsError:
                pass
        else:
            logging.warning("Can't find script %s", script_path)

    def install_grub2_bios(self):
        """ Install Grub2 bootloader in a BIOS system """
        grub_location = self.settings.get('bootloader_device')
        txt = _("Installing GRUB(2) BIOS boot loader in {0}").format(grub_location)
        logging.info(txt)

        # /dev and others need to be mounted (binded).
        # We call mount_special_dirs here just to be sure
        chroot.mount_special_dirs(self.dest_dir)

        grub_install = ['grub-install', '--directory=/usr/lib/grub/i386-pc', '--target=i386-pc',
                        '--boot-directory=/boot', '--recheck']

        if len(grub_location) > len("/dev/sdX"):  # ex: /dev/sdXY > 8
            grub_install.append("--force")

        grub_install.append(grub_location)

        chroot.run(grub_install, self.dest_dir)

        self.install_grub2_locales()

        # self.copy_grub2_theme_files()

        # Add -l option to os-prober's umount call so that it does not hang
        self.apply_osprober_patch()

        # Run grub-mkconfig last
        locale = self.settings.get("locale")
        try:
            cmd = ['sh', '-c', 'LANG={0} grub-mkconfig -o /boot/grub/grub.cfg'.format(locale)]
            chroot.run(cmd, self.dest_dir, 300)
        except subprocess.TimeoutExpired:
            msg = _("grub-mkconfig does not respond. Killing grub-mount and os-prober so we can continue.")
            logging.error(msg)
            subprocess.check_call(['killall', 'grub-mount'])
            subprocess.check_call(['killall', 'os-prober'])

        cfg = os.path.join(self.dest_dir, "boot/grub/grub.cfg")
        with open(cfg) as grub_cfg:
            if "Manjaro" in grub_cfg.read():
                txt = _("GRUB(2) BIOS has been successfully installed.")
                logging.info(txt)
                self.settings.set('bootloader_installation_successful', True)
            else:
                txt = _("ERROR installing GRUB(2) BIOS.")
                logging.warning(txt)
                self.settings.set('bootloader_installation_successful', False)

    @staticmethod
    def random_generator(size=4, chars=string.ascii_lowercase + string.digits):
        """ Generates a random string to be used as an identifier for the UEFI bootloader_id """
        return ''.join(random.choice(chars) for x in range(size))

    def install_grub2_efi(self):
        """ Install Grub2 bootloader in a UEFI system """
        uefi_arch = "x86_64"
        spec_uefi_arch = "x64"
        spec_uefi_arch_caps = "X64"
        bootloader_id = 'manjaro_grub' if not os.path.exists('/install/boot/EFI/manjaro_grub') else \
            'manjaro_grub_{0}'.format(self.random_generator())

        txt = _("Installing GRUB(2) UEFI {0} boot loader").format(uefi_arch)
        logging.info(txt)

        grub_install = [
            'grub-install',
            '--target={0}-efi'.format(uefi_arch),
            '--efi-directory=/install/boot',
            '--bootloader-id={0}'.format(bootloader_id),
            '--boot-directory=/install/boot',
            '--recheck']
        load_module = ['modprobe', '-a', 'efivarfs']

        try:
            subprocess.call(load_module, timeout=15)
            subprocess.check_call(grub_install, shell=True, timeout=120)
        except subprocess.CalledProcessError as process_error:
            logging.error('Command grub-install failed. Error output: %s', process_error.output)
        except subprocess.TimeoutExpired:
            logging.error('Command grub-install timed out.')
        except Exception as general_error:
            logging.error('Command grub-install failed. Unknown Error: %s', general_error)

        self.install_grub2_locales()

        # self.copy_grub2_theme_files()

        # Copy grub into dirs known to be used as default by some OEMs if they do not exist yet.
        grub_defaults = [os.path.join(self.dest_dir, "boot/EFI/BOOT", "BOOT{0}.efi".format(spec_uefi_arch_caps)),
                         os.path.join(self.dest_dir, "boot/EFI/Microsoft/Boot", 'bootmgfw.efi')]

        grub_path = os.path.join(self.dest_dir, "boot/EFI/manjaro_grub", "grub{0}.efi".format(spec_uefi_arch))

        for grub_default in grub_defaults:
            path = grub_default.split()[0]
            if not os.path.exists(path):
                msg = _("No OEM loader found in %s. Copying Grub(2) into dir.")
                logging.info(msg, path)
                os.makedirs(path)
                msg_failed = _("Copying Grub(2) into OEM dir failed: %s")
                try:
                    shutil.copy(grub_path, grub_default)
                except FileNotFoundError:
                    logging.warning(msg_failed, _("File not found."))
                except FileExistsError:
                    logging.warning(msg_failed, _("File already exists."))
                except Exception as general_error:
                    logging.warning(msg_failed, general_error)

        '''# Copy uefi shell if none exists in /boot/EFI
        shell_src = "/usr/share/thus/grub2-theme/shellx64_v2.efi"
        shell_dst = os.path.join(self.dest_dir, "boot/EFI/")
        try:
            shutil.copy2(shell_src, shell_dst)
        except FileNotFoundError:
            logging.warning(_("UEFI Shell drop-in not found at %s"), shell_src)
        except FileExistsError:
            pass
        except Exception as general_error:
            logging.warning(_("UEFI Shell drop-in could not be copied."))
            logging.warning(general_error)'''

        # Run grub-mkconfig last
        logging.info(_("Generating grub.cfg"))

        # /dev and others need to be mounted (binded).
        # We call mount_special_dirs here just to be sure
        chroot.mount_special_dirs(self.dest_dir)

        # Add -l option to os-prober's umount call so that it does not hang
        self.apply_osprober_patch()

        locale = self.settings.get("locale")
        try:
            cmd = ['sh', '-c', 'LANG={0} grub-mkconfig -o /boot/grub/grub.cfg'.format(locale)]
            chroot.run(cmd, self.dest_dir, 300)
        except subprocess.TimeoutExpired:
            txt = _("grub-mkconfig appears to be hung. Killing grub-mount and os-prober so we can continue.")
            logging.error(txt)
            subprocess.check_call(['killall', 'grub-mount'])
            subprocess.check_call(['killall', 'os-prober'])

        paths = [os.path.join(self.dest_dir, "boot/grub/x86_64-efi/core.efi"),
                 os.path.join(self.dest_dir, "boot/EFI/{0}".format(bootloader_id),
                              "grub{0}.efi".format(spec_uefi_arch))]

        exists = False

        for path in paths:
            if os.path.exists(path):
                exists = True
            else:
                exists = False

        if exists:
            txt = _("GRUB(2) UEFI install completed successfully")
            logging.info(txt)
            self.settings.set('bootloader_installation_successful', True)
        else:
            txt = _("GRUB(2) UEFI install may not have completed successfully.")
            logging.warning(txt)
            self.settings.set('bootloader_installation_successful', False)

    def apply_osprober_patch(self):
        """ Adds -l option to os-prober's umount call so that it does not hang """
        osp_path = os.path.join(self.dest_dir, "usr/lib/os-probes/50mounted-tests")
        if os.path.exists(osp_path):
            with open(osp_path) as osp:
                text = osp.read().replace("umount", "umount -l")
            with open(osp_path, "w") as osp:
                osp.write(text)
            logging.debug(_("50mounted-tests file patched successfully"))
        else:
            logging.warning(_("Failed to patch 50mounted-tests, file not found."))

    def copy_grub2_theme_files(self):
        """ Copy grub2 theme files to /boot """
        logging.info(_("Copying GRUB(2) Theme Files"))
        theme_dir_src = "/usr/share/thus/grub2-theme/Manjaro-Default"
        theme_dir_dst = os.path.join(self.dest_dir, "boot/grub/themes/Manjaro-Default")
        try:
            shutil.copytree(theme_dir_src, theme_dir_dst)
        except FileNotFoundError:
            logging.warning(_("Grub2 theme files not found"))
        except FileExistsError:
            logging.warning(_("Grub2 theme files already exist."))

    def install_grub2_locales(self):
        """ Install Grub2 locales """
        logging.info(_("Installing Grub2 locales."))
        dest_locale_dir = os.path.join(self.dest_dir, "boot/grub/locale")

        if not os.path.exists(dest_locale_dir):
            os.makedirs(dest_locale_dir)

        grub_mo = os.path.join(self.dest_dir, "usr/share/locale/en@quot/LC_MESSAGES/grub.mo")

        try:
            shutil.copy2(grub_mo, os.path.join(dest_locale_dir, "en.mo"))
        except FileNotFoundError:
            logging.warning(_("Can't install GRUB(2) locale."))
        except FileExistsError:
            # Ignore if already exists
            pass

    def install_gummiboot(self):
        """ Install Gummiboot bootloader to the EFI System Partition """
        # Setup bootloader menu
        menu_dir = os.path.join(self.dest_dir, "boot/loader")
        os.makedirs(menu_dir)
        menu_path = os.path.join(menu_dir, "loader.conf")
        with open(menu_path, "w") as menu_file:
            menu_file.write("default manjaro")

        # Setup boot entries

        if not self.settings.get('use_luks'):
            conf = []
            conf.append("title\tManjaro\n")
            conf.append("linux\t/{0}\n".format(self.vmlinuz))
            conf.append("initrd\t/{0}.img\n".format(self.initramfs))
            conf.append("options\troot=UUID={0} rw\n\n".format(self.root_uuid))
            conf.append("title\tManjaro (fallback)\n")
            conf.append("linux\t/{0}\n".format(self.vmlinuz))
            conf.append("initrd\t/{0}-fallback.img\n".format(self.initramfs))
            conf.append("options\troot=UUID={0} rw\n\n".format(self.root_uuid))
        else:
            luks_root_volume = self.settings.get('luks_root_volume')

            # In automatic mode, root_device is in self.mount_devices, as it should be
            root_device = self.root_device

            if self.method == "advanced" and self.settings.get('use_luks_in_root'):
                root_device = self.settings.get('luks_root_device')

            root_uuid = fs.get_info(root_device)['UUID']

            key = ""
            if self.settings.get("luks_root_password") == "":
                key = "cryptkey=UUID={0}:ext2:/.keyfile-root".format(self.boot_uuid)

            root_uuid_line = "cryptdevice=UUID={0}:{1} {2} root=UUID={3} rw"
            root_uuid_line = root_uuid_line.format(root_uuid, luks_root_volume, key, root_uuid)

            conf = []
            conf.append("title\tManjaro\n")
            conf.append("linux\t/boot/{0}\n".format(self.vmlinuz))
            conf.append("options\tinitrd=/boot/{0}.img {1}\n\n".format(self.initramfs,root_uuid_line))
            conf.append("title\tManjaro (fallback)\n")
            conf.append("linux\t/boot/{0}\n".format(self.vmlinuz))
            conf.append("options\tinitrd=/boot/{0}-fallback.img {1}\n\n".format(self.initramfs,root_uuid_line))

        # Write boot entries
        entries_dir = os.path.join(self.dest_dir, "boot/loader/entries")
        os.makedirs(entries_dir)
        entry_path = os.path.join(entries_dir, "manjaro.conf")
        with open(entry_path, "w") as entry_file:
            for line in conf:
                entry_file.write(line)

        # Install bootloader

        try:
            efi_system_partition = os.path.join(self.dest_dir, "boot")
            cmd = ['gummiboot', '--path={0}'.format(efi_system_partition), 'install']
            subprocess.check_call(cmd)
            logging.info(_("Gummiboot install completed successfully"))
            self.settings.set('bootloader_installation_successful', True)
        except subprocess.CalledProcessError as process_error:
            logging.error(process_error)
            logging.warning(_("Gummiboot install has NOT completed successfully!"))
            self.settings.set('bootloader_installation_successful', False)

    def freeze_unfreeze_xfs(self):
        """ Freeze and unfreeze xfs, as hack for grub(2) installing """
        if not os.path.exists("/usr/bin/xfs_freeze"):
            return

        xfs_boot = False
        xfs_root = False

        try:
            subprocess.check_call(["sync"])
            with open("/proc/mounts", "r") as mounts_file:
                mounts = mounts_file.readlines()
            # We leave a blank space in the end as we want to search exactly for this mount points
            boot_mount_point = self.dest_dir + "/boot "
            root_mount_point = self.dest_dir + " "
            for line in mounts:
                if " xfs " in line:
                    if boot_mount_point in line:
                        xfs_boot = True
                    elif root_mount_point in line:
                        xfs_root = True
            if xfs_boot:
                boot_mount_point = boot_mount_point.rstrip()
                subprocess.check_call(["xfs_freeze", "-f", boot_mount_point])
                subprocess.check_call(["xfs_freeze", "-u", boot_mount_point])
            if xfs_root:
                subprocess.check_call(["xfs_freeze", "-f", self.dest_dir])
                subprocess.check_call(["xfs_freeze", "-u", self.dest_dir])
        except subprocess.CalledProcessError as process_error:
            logging.warning(_("Can't freeze/unfreeze xfs system"))
            logging.warning(process_error)
