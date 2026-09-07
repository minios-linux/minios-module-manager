import os
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from minios_module_manager.model import ModuleRecord
from minios_module_manager.ui import module_presentation_order


class ModulePresentationOrderTests(unittest.TestCase):
    def test_running_custom_modules_follow_system_modules(self):
        modules = (
            ModuleRecord('00-core.sb', source='/data/minios/00-core.sb'),
            ModuleRecord('packages.sb', source='/data/minios/modules/packages.sb'),
            ModuleRecord('virt-viewer.sb', source='/data/minios/modules/virt-viewer.sb'),
            ModuleRecord('01-kernel.sb', source='/data/minios/01-kernel.sb'),
            ModuleRecord('02-firmware.sb', source='/data/minios/02-firmware.sb'),
        )

        ordered = module_presentation_order(modules)

        self.assertEqual([module.name for module in ordered], [
            '00-core.sb', '01-kernel.sb', '02-firmware.sb',
            'packages.sb', 'virt-viewer.sb',
        ])

    def test_order_within_each_group_is_stable(self):
        modules = (
            ModuleRecord('custom-z.sb', origin='modules'),
            ModuleRecord('02-firmware.sb', origin='base'),
            ModuleRecord('custom-a.sb', origin='persistence'),
            ModuleRecord('00-core.sb', origin='base'),
        )

        ordered = module_presentation_order(modules)

        self.assertEqual([module.name for module in ordered], [
            '02-firmware.sb', '00-core.sb', 'custom-z.sb', 'custom-a.sb',
        ])


if __name__ == '__main__':
    unittest.main()
